"""The Discord REST transport — a dumb pipe, in-process, over `urllib`. Decides nothing: fast-path
dispatch and Gateway session logic live in `gateway.py`; this only puts bytes on the wire and
normalizes what comes back.

`_normalize` is a pure function of one Gateway dispatch payload (`{"t": ..., "d": ...}`), so the
dispatch → ingest path is unit-testable with no network. It maps MESSAGE_CREATE to our event shape
(text|command|photo), dropping the bot's own messages (echoed back over the Gateway like any other
message in the channel) and any dispatch kind we don't care about. A button click
(INTERACTION_CREATE, type 3 = MESSAGE_COMPONENT) becomes an `action` event carrying the
interaction id/token gateway.py needs to
acknowledge it — Discord requires a REST acknowledgment even for a Gateway-delivered interaction.
`event_id` is cast to `int`: `channel_inbox.event_id` is an INTEGER UNIQUE column and Discord ids
arrive as decimal-string snowflakes, which always fit a 64-bit integer.

Outbound text is chunked at Discord's 2000-char message limit; a control spec attaches to the last
chunk as a single action row of buttons.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from sellee import __version__
from sellee.channel import controls

# Required, in this documented shape, and enforced at the edge: urllib's default
# `Python-urllib/x.y` is blocklisted, so the API and the CDN both 403 (Cloudflare 1010) before the
# token is read — indistinguishable from a rejected token. Enforcement is reputation-driven, so it
# passes from some networks and not others.
USER_AGENT = f"DiscordBot (https://github.com/carousell/sellee, {__version__})"

MAX_TEXT_LEN = 2000
_ACTION_ROW = 1
_BUTTON = 2
_BUTTON_STYLE_PRIMARY = 1
# Discord's own cap: an action row holds at most five buttons, and a sixth is a rejected message,
# not a wrapped one. The shared legibility cap in `channel.controls` is stricter, so in practice
# this never binds — it stays because it is the API's limit rather than our taste, and
# `build_components` takes the tighter of the two so raising the shared one can never reach it.
MAX_BUTTONS_PER_ROW = 5


class ChannelError(Exception):
    """A transport failure surfaced token-free by construction — every raise below carries only
    the HTTP method/path and status, never the Authorization header."""


def chunk_text(text: str, limit: int = MAX_TEXT_LEN) -> list:
    remaining = text or ""
    chunks: list = []
    while len(remaining) > limit:
        split = remaining.rfind("\n", 0, limit)
        if split <= 0:
            head, remaining = remaining[:limit], remaining[limit:]
        else:
            head, remaining = remaining[:split], remaining[split + 1 :]
        chunks.append(head)
    chunks.append(remaining)
    return chunks


def build_components(spec: list) -> list:
    """A provider-neutral (label, custom_id) control spec into action rows of buttons.

    Wrapped rather than truncated: a control the core emitted is always rendered, or the seller is
    looking at a door they cannot open. Discord also rejects an action row holding more than five
    buttons, and the marketplace picker is as long as the seller's enabled list.

    The packing itself is `channel.controls.wrap` — shared with Telegram, because how wide a label
    reads is a property of the label, not of the provider, and two copies of that judgement drift.
    """
    rows = controls.wrap(spec, max_per_row=min(controls.MAX_BUTTONS_PER_ROW, MAX_BUTTONS_PER_ROW))
    return [
        {
            "type": _ACTION_ROW,
            "components": [
                {
                    "type": _BUTTON,
                    "style": _BUTTON_STYLE_PRIMARY,
                    "label": label,
                    "custom_id": token,
                }
                for label, token in row
            ],
        }
        for row in rows
    ]


def _epoch(timestamp: str | None) -> float | None:
    """Discord's ISO 8601 message timestamp as a float. `src_ts` is a REAL column and rides on
    `channel.in` bus events, so a provider that left the string in place would be the only one
    putting text in a numeric field. An unparseable value degrades to None — src_ts is
    informational, never the ordering clock, so it is not worth failing an ingest over."""
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp).timestamp()
    except ValueError:
        return None


def _normalize(event: dict) -> dict | None:
    kind = event.get("t")
    data = event.get("d") or {}
    if kind == "MESSAGE_CREATE":
        if (data.get("author") or {}).get("bot"):
            return None  # the bot's own sends, echoed back like any other channel message
        attachments = data.get("attachments") or []
        if attachments:
            largest = max(attachments, key=lambda a: a.get("size", 0))
            return {
                "event_id": int(data["id"]),
                "kind": "photo",
                "text": data.get("content", ""),
                "payload": {"url": largest["url"], "filename": largest.get("filename")},
                "src_ts": _epoch(data.get("timestamp")),
            }
        content = data.get("content", "")
        if content.startswith("/"):
            # No slash-command menu on this provider — "/" text is lifted to a command, truncated
            # to the leading token so fastpaths matches it.
            command, _, _argument = content.partition(" ")
            return {
                "event_id": int(data["id"]),
                "kind": "command",
                "text": command,
                "payload": {},
                "src_ts": _epoch(data.get("timestamp")),
            }
        return {
            "event_id": int(data["id"]),
            "kind": "text",
            "text": content,
            "payload": {},
            "src_ts": _epoch(data.get("timestamp")),
        }
    if kind == "INTERACTION_CREATE" and data.get("type") == 3:  # MESSAGE_COMPONENT
        custom_id = (data.get("data") or {}).get("custom_id", "")
        ref, choice = custom_id.split(":", 1) if ":" in custom_id else (None, custom_id)
        return {
            "event_id": int(data["id"]),
            "kind": "action",
            "text": choice,
            "payload": {
                "ref": ref,
                "choice": choice,
                "interaction_id": data["id"],
                "interaction_token": data["token"],
            },
            "src_ts": None,
        }
    return None


class DiscordClient:
    """REST API client bound to one bot token. Every method raises ChannelError (token-free) on a
    transport or API-level failure."""

    def __init__(
        self, token: str, *, api_base: str = "https://discord.com/api/v10", timeout: float = 60.0
    ):
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> object:
        url = f"{self._api_base}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bot {self._token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 our URL
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise ChannelError(f"Discord API {method} {path} HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ChannelError(
                f"Discord API {method} {path} network error: {exc.__class__.__name__}"
            ) from None

    def get_me(self) -> dict:
        return self._request("GET", "/users/@me")

    def get_application(self) -> dict:
        return self._request("GET", "/oauth2/applications/@me")

    def send_message(self, channel_id: int, text: str, *, components: list | None = None) -> int:
        """Chunked at the 2000-char limit; the control spec (if any) attaches to the last chunk
        only. Returns the last sent message's id."""
        chunks = chunk_text(text)
        message_id = None
        for i, chunk in enumerate(chunks):
            body: dict = {"content": chunk}
            if components is not None and i == len(chunks) - 1:
                body["components"] = build_components(components)
            result = self._request("POST", f"/channels/{channel_id}/messages", body)
            message_id = int(result["id"]) if isinstance(result, dict) and "id" in result else None
        return message_id

    def trigger_typing(self, channel_id: int) -> None:
        self._request("POST", f"/channels/{channel_id}/typing")

    def acknowledge_interaction(
        self, interaction_id, interaction_token: str, *, clear_components: bool = False
    ) -> None:
        """Acknowledge a button click.

        An interaction gets exactly one initial response, so the two shapes are exclusive:

        * type 6 (DEFERRED_UPDATE_MESSAGE) — ack with no loading state and no edit. The fast-path
          reply arrives as an ordinary follow-up message.
        * type 7 (UPDATE_MESSAGE) with empty components — ack *and* take the buttons off, in one
          request. An answered ask wants both, and doing it here costs the Gateway pump thread
          nothing extra, which a second REST call would.
        """
        body = {"type": 7, "data": {"components": []}} if clear_components else {"type": 6}
        self._request("POST", f"/interactions/{interaction_id}/{interaction_token}/callback", body)

    def download_attachment(self, url: str, dest: Path) -> Path:
        """Discord attachment URLs are pre-signed/public CDN links — no Authorization header
        needed or sent, deliberately: the bot token must never ride on a URL that could be logged
        or cached by an intermediary. The User-Agent still goes: the CDN sits behind the same
        edge filter as the API."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 our URL
                dest.write_bytes(resp.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ChannelError(f"attachment download failed: {exc.__class__.__name__}") from None
        return dest
