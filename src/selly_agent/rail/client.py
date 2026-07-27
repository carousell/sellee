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
_CLIENT_UA = "SELLY-rail/1"


class RailError(Exception):
    """Base for rail failures. Its message is caller-facing and carries no secret."""


class RailUnprovisioned(RailError):
    """No carousell.ai API key is present — provisioning must run first."""


class RailAuthError(RailError):
    """The rail rejected our credentials (401/403)."""


class RailNetworkError(RailError):
    """The rail was unreachable, timed out, or returned an unparseable response."""


class RailToolError(RailError):
    """The rail accepted the request but the tool call itself failed."""


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
            raise RailNetworkError(f"rail returned HTTP {exc.code}") from exc
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
            raise RailToolError(message)
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
                "clientInfo": {"name": "selly-agent", "version": "1"},
            },
        )

    def call_tool(self, name: str, arguments: dict) -> dict:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RailToolError(_text_content(result) or f"{name} failed")
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
        carries no id — without one there is no listing to point at."""
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

    def update_listing(self, listing_id: str, *, status: str) -> dict:
        """Flip a listing's status on the rail. PATCH semantics: only what is passed changes, so
        this leaves the photos, price and text alone."""
        return self.call_tool("update_listing", {"id": str(listing_id), "status": status})

    def create_checkout(self, args: dict) -> dict:
        """Mint a checkout link for a listing at an agreed price. Returns {checkout_url}. Raises
        RailToolError if the rail returns no URL (we never fabricate one)."""
        result = self.call_tool("create_checkout", args)
        url = result.get("checkout_url") or result.get("url")
        if not url:
            raise RailToolError("create_checkout returned no checkout URL")
        return {"checkout_url": url}

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


def _text_content(result: dict) -> str:
    """Concatenate the text of an MCP tool result's content array, if any."""
    parts = []
    for block in result.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)
