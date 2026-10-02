"""Minimal stdlib MCP client for the carousell.ai rail, plus the live listing-URL verify.

We are the client here (unidirectional): POST JSON-RPC to <api_base>/mcp with the guest key as a
bearer token, initialize, then tools/call. The API key travels only in the Authorization header —
never in argv, never logged. Failures are typed (auth / network / tool) so callers can respond
precisely. verify_listing_url is the fail-closed gate: a recorded URL must sit under
<web_base_url>/listing/ and return HTTP 200 right now, or nothing is recorded.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

_DEFAULT_TIMEOUT_SEC = 15.0
_VERIFY_TIMEOUT_SEC = 5.0
_LISTING_PATH = "/listing/"
# carousell.ai sits behind Cloudflare, which 403s the default urllib User-Agent. A plain browser
# UA is accepted for the read-only, no-auth verify GET.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
_CLIENT_UA = "SELLEE-rail/1"


class RailError(Exception):
    """Base for rail failures. Its message is caller-facing and carries no secret."""


class RailUnprovisioned(RailError):
    """No carousell.ai API key is present — provisioning must run first."""


class RailAuthError(RailError):
    """The rail rejected our credentials (401/403)."""


class RailNetworkError(RailError):
    """The rail was unreachable, timed out, or returned an unparseable response."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        # The HTTP status the rail answered with, when it answered at all.
        self.status = status


class RailToolError(RailError):
    """The rail accepted the request but the tool call itself failed."""


class RailToolRefused(RailToolError):
    """The rail itself answered the call with an error, rather than an answer we could not read."""


class RailClient:
    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        web_base_url: str,
        timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
    ):
        self._endpoint = api_base.rstrip("/") + "/mcp"
        self._api_key = api_key
        self._web_base_url = web_base_url.rstrip("/")
        self._timeout = timeout_sec
        self._next_id = 0

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        body = json.dumps(
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}}
        ).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                # Both are required even though the server answers with plain JSON; it rejects the
                # request otherwise, and the rejection lands after auth so a bad key masks it.
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self._api_key}",
                "User-Agent": _CLIENT_UA,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RailAuthError("carousell.ai rejected the guest key") from exc
            raise RailNetworkError(f"rail returned HTTP {exc.code}", status=exc.code) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RailNetworkError(f"rail unreachable: {type(exc).__name__}") from exc
        try:
            envelope = json.loads(raw)
        except ValueError as exc:
            raise RailNetworkError("rail returned an unparseable response") from exc
        if not isinstance(envelope, dict):
            raise RailNetworkError("rail response is not a JSON-RPC object")
        if envelope.get("error"):
            message = str(envelope["error"].get("message", "rail error"))
            raise RailToolRefused(message)
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise RailNetworkError("rail response has no result object")
        return result

    def initialize(self) -> dict:
        return self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "sellee", "version": "1"},
            },
        )

    def offers_tool(self, name: str) -> bool:
        """Whether the server lists this tool. bazaar registers some tools only when a feature is
        on, such as photo upload when media is enabled."""
        tools = self._rpc("tools/list").get("tools") or []
        return any(isinstance(t, dict) and t.get("name") == name for t in tools)

    def call_tool(self, name: str, arguments: dict) -> dict:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RailToolRefused(_text_content(result) or f"{name} failed")
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        text = _text_content(result)
        if text:
            try:
                parsed = json.loads(text)
            except ValueError as exc:
                raise RailToolError(f"{name} returned non-JSON content") from exc
            if isinstance(parsed, dict):
                return parsed
        raise RailToolError(f"{name} returned no usable content")

    def listing_url(self, listing_id: str) -> str:
        """The listing's page URL, composed from the id the rail assigned it."""
        return self._web_base_url + _LISTING_PATH + str(listing_id)

    def create_listing(self, args: dict) -> dict:
        """Create a listing and return {listing_id, url}. Raises RailToolError when the response
        carries no id. The currency is not read back: the request asserts the recorded one."""
        result = self.call_tool("create_listing", args)
        listing = result.get("listing")
        listing = listing if isinstance(listing, dict) else result
        listing_id = listing.get("id") or listing.get("listing_id")
        if not listing_id:
            raise RailToolError("create_listing returned no listing id")
        url = listing.get("url") or listing.get("listing_url") or self.listing_url(listing_id)
        return {"listing_id": listing_id, "url": url}

    def upload_photo(self, data: bytes, content_type: str) -> str:
        """Mint a short-lived upload URL, POST the image bytes to it, and return the encrypted
        media reference create_listing wants.

        The reference comes back from the upload, not from the mint. That URL is pre-signed and
        takes no Authorization header, so our key never travels to the media host.
        """
        minted = self.call_tool("create_photo_upload_url", {})
        upload_url = minted.get("upload_url") or minted.get("url")
        if not upload_url:
            raise RailToolError("create_photo_upload_url returned no upload URL")
        req = urllib.request.Request(
            upload_url,
            data=data,
            method="POST",
            headers={"Content-Type": content_type, "User-Agent": _CLIENT_UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise RailToolError(f"photo upload returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RailNetworkError(f"photo upload unreachable: {type(exc).__name__}") from exc
        if status not in (200, 201, 204):
            raise RailToolError(f"photo upload returned HTTP {status}")
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise RailToolError("photo upload returned an unparseable response") from exc
        encrypted = payload.get("encrypted_url") or payload.get("media_url")
        if not encrypted:
            raise RailToolError("photo upload returned no media reference")
        return encrypted

    def update_listing(
        self,
        listing_id: str,
        *,
        status: str | None = None,
        external_urls: dict | None = None,
        title: str | None = None,
        description: str | None = None,
        price_cents: int | None = None,
        media: dict | None = None,
    ) -> dict:
        """Change a listing on the rail. PATCH semantics all the way through: an argument not
        passed is left out of the call and stays unchanged, so a status flip never touches the
        cross-links, a cross-link push never touches status, and a price edit touches nothing but
        the price. `external_urls` and `media` each replace the rail's whole set — `{"urls": []}`
        clears it, which is not the same as leaving it out.

        There is no `currency`: the rail fixes it at create and its update verb does not take one.
        """
        fields = {
            "status": status,
            "external_urls": external_urls,
            "title": title,
            "description": description,
            "price_cents": price_cents,
            "media": media,
        }
        present = {name: value for name, value in fields.items() if value is not None}
        if not present:
            raise ValueError("update_listing needs at least one field to change")
        return self.call_tool("update_listing", {"id": str(listing_id), **present})

    def create_checkout(self, args: dict) -> dict:
        """Mint a checkout link for a listing at an agreed price. Returns {checkout_url}. Raises
        RailToolError if the rail returns no URL (we never fabricate one)."""
        result = self.call_tool("create_checkout", args)
        url = result.get("checkout_url") or result.get("url")
        if not url:
            raise RailToolError("create_checkout returned no checkout URL")
        return {"checkout_url": url}

    def create_promotion_url(self) -> dict:
        """Mint the seller's one-time sign-in link (the rail's "promotion") for the account the
        API key belongs to. Returns {promotion_url}; raises RailToolError when the rail returns no
        URL (we never fabricate one). An already-signed-in account surfaces as
        RailToolError("already a seller") — interpreting that is the caller's job."""
        result = self.call_tool("create_promotion_url", {})
        url = result.get("promotion_url")
        if not url:
            raise RailToolError("create_promotion_url returned no promotion URL")
        return {"promotion_url": url}

    def list_threads(self, cursor: str, limit: int) -> dict:
        """The seller's relay threads changed since `cursor`, as {threads, next_cursor}. At least
        once: a thread can come back on a later page, so the caller drops repeats."""
        args: dict = {"limit": limit}
        if cursor:
            args["cursor"] = cursor
        result = self.call_tool("list_threads", args)
        if not isinstance(result.get("threads", []), list):
            raise RailToolError("list_threads returned no thread list")
        return {
            "threads": result.get("threads") or [],
            "next_cursor": result.get("next_cursor", ""),
        }

    def get_thread(self, thread_id: str) -> dict:
        """One relay thread and all its messages in the order bazaar stored them."""
        result = self.call_tool("get_thread", {"id": str(thread_id)})
        if not isinstance(result.get("thread"), dict):
            raise RailToolError("get_thread returned no thread")
        return {"thread": result["thread"], "messages": result.get("messages") or []}

    def reply_to_thread(self, thread_id: str, text: str, client_message_id: str) -> dict:
        """Reply to the buyer on a relay thread. bazaar stores one reply per client_message_id,
        so a retry under the same id returns the same {message_id} and sends nothing twice."""
        result = self.call_tool(
            "reply_to_thread",
            {"id": str(thread_id), "text": text, "client_message_id": client_message_id},
        )
        if not result.get("message_id"):
            raise RailToolError("reply_to_thread returned no message id")
        return {"message_id": result["message_id"]}

    def verify_listing_url(self, url: str) -> None:
        """Fail-closed live check: the URL must sit under <web_base_url>/listing/ and return HTTP
        200 right now (urllib follows the id->slug 301). Raises RailToolError otherwise."""
        url = (url or "").strip()
        prefix = self._web_base_url + _LISTING_PATH
        if not url.startswith(prefix):
            raise RailToolError(f"listing url is not under {prefix!r}")
        req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
        try:
            with urllib.request.urlopen(req, timeout=_VERIFY_TIMEOUT_SEC) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
        except urllib.error.HTTPError as exc:
            raise RailToolError(f"listing page returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RailToolError(f"listing page not reachable: {type(exc).__name__}") from exc
        if status != 200:
            raise RailToolError(f"listing page returned HTTP {status}")


def listing_id_from_url(url) -> str:
    """The id back out of a listing page URL — the inverse of RailClient.listing_url. Tolerates a
    slug or query suffix after the id; returns "" when there is no /listing/ segment to read."""
    url = str(url or "")
    if _LISTING_PATH not in url:
        return ""
    return url.split(_LISTING_PATH, 1)[1].split("/", 1)[0].split("?", 1)[0].strip()


def _text_content(result: dict) -> str:
    """Concatenate the text of an MCP tool result's content array, if any."""
    parts = []
    for block in result.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)
