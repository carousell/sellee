"""The Telegram Bot API transport — a dumb pipe, in-process, one live consumer.

A thin class over urllib: it sends/receives over `<api_base>/bot<token>/<METHOD>` and decides
nothing (all logic lives in the poller and the fast-path commands). Two disciplines are
load-bearing here:

  * The token never leaks. It rides in the URL path, so any error text is stripped of the URL by
    construction — a ChannelError carries the method and status, never the token.
  * `_normalize` is a pure function of one update, so the poll/ingest path is unit-testable with
    no network. It maps an update to our event shape (command|text|action|photo), carrying
    Telegram's own `date` as `src_ts` (informational only — never the ordering clock), and lifts a
    `/start <payload>` into payload.start_param so the nonce bind can authenticate it.

Outbound text is chunked at Telegram's 4096-char hard limit (the API rejects longer), and an
inline keyboard's callback_data is capped at 64 bytes (the API's limit) at build time.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

# Telegram's hard limits. A longer message is rejected by the API; a longer callback_data is
# rejected at send. We enforce both at our boundary so a failure is caught here, not on the wire.
MAX_TEXT_LEN = 4096
MAX_CALLBACK_BYTES = 64

# Headroom over the long poll's own timeout, so the socket outlives the poll the API is holding
# open rather than racing it.
LONG_POLL_SLACK_SEC = 15.0

# A Telegram bot token is `<bot_id>:<auth>` — digits, a colon, ~35 chars of [A-Za-z0-9_-]. A cheap
# offline pre-check so an obviously-wrong paste fails clearly before the network; getMe is the
# authoritative test.
_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")


class ChannelError(Exception):
    """A transport failure surfaced token-free by construction (the URL carrying the token is
    never echoed into the message)."""


def is_valid_token_format(token: str) -> bool:
    """True if `token` has the shape of a Telegram bot token. Pure + offline (testable)."""
    return bool(_TOKEN_RE.match((token or "").strip()))


def chunk_text(text: str, limit: int = MAX_TEXT_LEN) -> list:
    """Split text into <=limit-char chunks, preferring a newline boundary. Always returns at least
    one chunk (an empty string for empty text) so a send is never silently dropped."""
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


def build_inline_keyboard(rows: list) -> dict:
    """Build an InlineKeyboardMarkup from rows of (label, callback_data) pairs. Enforces the
    64-byte callback_data cap here so an over-long datum fails at build, not on the wire."""
    keyboard = []
    for row in rows:
        buttons = []
        for label, callback_data in row:
            if len(callback_data.encode("utf-8")) > MAX_CALLBACK_BYTES:
                raise ValueError(
                    f"callback_data exceeds {MAX_CALLBACK_BYTES} bytes: {callback_data!r}"
                )
            buttons.append({"text": label, "callback_data": callback_data})
        keyboard.append(buttons)
    return {"inline_keyboard": keyboard}


def commands_hash(commands: list) -> str:
    """A stable short hash of a command set, so setMyCommands is skipped when nothing changed."""
    blob = json.dumps(commands, sort_keys=True).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:12]


def _normalize(update: dict, authorized_chat: int | None) -> tuple:
    """Turn one Telegram update into (event, chat_id), or (None, chat_id) when it is not from the
    authorized chat. The event is {event_id, kind, text, payload, src_ts}; kind is
    command|text|action|photo. Pure — no network, no state.

    A `/start <payload>` is normalized to text "/start" with payload.start_param set to the
    argument (the deep-link nonce), so bind authentication is a payload compare, not a text parse.
    """
    if "callback_query" in update:
        cq = update["callback_query"]
        chat = cq.get("message", {}).get("chat", {}).get("id")
        if authorized_chat is not None and chat != authorized_chat:
            return None, chat
        data = cq.get("data", "")
        ref, choice = data.split(":", 1) if ":" in data else (None, data)
        return (
            {
                "event_id": update["update_id"],
                "kind": "action",
                "text": choice,
                "payload": {
                    "ref": ref,
                    "choice": choice,
                    "callback_query_id": cq.get("id"),
                    # The message the button sits on, so an answered ask can have its keyboard
                    # taken away. Carried on the row rather than looked up later: the notice never
                    # records the message id it was delivered as, and the tap already knows it.
                    "message_id": cq.get("message", {}).get("message_id"),
                },
                "src_ts": cq.get("message", {}).get("date"),
            },
            chat,
        )
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None, None
    chat = msg.get("chat", {}).get("id")
    if authorized_chat is not None and chat != authorized_chat:
        return None, chat
    if "photo" in msg:
        # The largest size is the full-resolution photo; smaller entries are Telegram's thumbnails.
        largest = max(msg["photo"], key=lambda p: p.get("file_size", 0))
        payload = {"file_id": largest["file_id"]}
        # media_group_id groups photos sent as one gallery selection — a batching hint only.
        if msg.get("media_group_id"):
            payload["media_group_id"] = msg["media_group_id"]
        return (
            {
                "event_id": update["update_id"],
                "kind": "photo",
                "text": msg.get("caption", ""),
                "payload": payload,
                "src_ts": msg.get("date"),
            },
            chat,
        )
    text = msg.get("text", "")
    if text.startswith("/"):
        command, _, argument = text.partition(" ")
        payload = {"start_param": argument.strip()} if command == "/start" else {}
        return (
            {
                "event_id": update["update_id"],
                "kind": "command",
                "text": command,
                "payload": payload,
                "src_ts": msg.get("date"),
            },
            chat,
        )
    return (
        {
            "event_id": update["update_id"],
            "kind": "text",
            "text": text,
            "payload": {},
            "src_ts": msg.get("date"),
        },
        chat,
    )


class TelegramClient:
    """Bot API transport bound to one token. Every method raises ChannelError (token-free) on a
    transport or API-level failure."""

    def __init__(
        self, token: str, *, api_base: str = "https://api.telegram.org", timeout: float = 60.0
    ):
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout

    def _api(self, method: str, params: dict, *, timeout: float | None = None) -> object:
        url = f"{self._api_base}/bot{self._token}/{method}"
        data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(  # noqa: S310 our URL
                req, timeout=self._timeout if timeout is None else timeout
            ) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            # The URL carries the token; never echo it. Report only the method and status.
            raise ChannelError(f"Bot API {method} HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ChannelError(
                f"Bot API {method} network error: {exc.__class__.__name__}"
            ) from None
        if not payload.get("ok"):
            desc = payload.get("description", "?")
            raise ChannelError(f"Bot API {method} returned error: {desc}")
        return payload["result"]

    def get_me(self) -> dict:
        return self._api("getMe", {})

    def get_updates(self, offset: int, timeout: int, allowed_updates: list) -> list:
        """Long-poll for updates. The socket timeout is derived from the poll rather than taken
        from the client's, because a client tuned short for its *sends* — the poller's is, since
        those run on the one thread consuming this — would otherwise abort the poll early and turn
        every long poll into a reconnect."""
        return self._api(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": allowed_updates},
            timeout=timeout + LONG_POLL_SLACK_SEC,
        )

    def send_message(self, chat_id: int, text: str, *, reply_markup: dict | None = None) -> list:
        """Send text (chunked at the 4096-char limit) to a chat. An inline keyboard, if any,
        attaches to the last chunk only. Returns the message_ids of the sent chunks."""
        chunks = chunk_text(text)
        message_ids: list = []
        for i, chunk in enumerate(chunks):
            params = {"chat_id": chat_id, "text": chunk}
            if reply_markup is not None and i == len(chunks) - 1:
                params["reply_markup"] = reply_markup
            result = self._api("sendMessage", params)
            if isinstance(result, dict):
                message_ids.append(result.get("message_id"))
        return message_ids

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self._api("sendChatAction", {"chat_id": chat_id, "action": action})

    def answer_callback_query(self, callback_query_id: str) -> None:
        self._api("answerCallbackQuery", {"callback_query_id": callback_query_id})

    def clear_inline_keyboard(self, chat_id: int, message_id: int) -> None:
        """Take the buttons off a message, leaving its text alone.

        An empty `inline_keyboard` rather than an omitted `reply_markup`: omitting it is "change
        nothing", which is not what an answered ask wants to say.
        """
        self._api(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
        )

    def set_my_commands(self, commands: list) -> None:
        self._api("setMyCommands", {"commands": commands})

    def get_file_and_download(self, file_id: str, dest: Path) -> Path:
        """Resolve a file_id to its path and download it to `dest`. getFile is cursor-free, so a
        crash before the ingest transaction re-downloads harmlessly."""
        result = self._api("getFile", {"file_id": file_id})
        file_path = result.get("file_path") if isinstance(result, dict) else None
        if not file_path:
            raise ChannelError("getFile returned no file_path")
        url = f"{self._api_base}/file/bot{self._token}/{file_path}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:  # noqa: S310 our URL
                dest.write_bytes(resp.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ChannelError(f"file download failed: {exc.__class__.__name__}") from None
        return dest
