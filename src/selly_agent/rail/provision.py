"""carousell.ai guest-key provisioning — zero-LLM, fail-soft, off the pass path.

POST /api/v1/guests {"country": <region>} returns {user_id, country, api_key}; the key is
stored 0600 through secrets.py and never printed. ensure is idempotent (a key already present
means no network call); reprovision forces a fresh key. Operational failures are returned as a
status dict with defer=True, never raised — a provisioning hiccup must not crash a caller.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from selly_agent import secrets

_GUESTS_PATH = "/api/v1/guests"
_DEFAULT_TIMEOUT_SEC = 10.0
# Cloudflare fronts api.carousell.ai and 403s urllib's default UA; send an honest, version-
# stamped one so the guests POST isn't blocked. No secret in it.
_USER_AGENT = "SELLY/1"


class ProvisionError(Exception):
    """An operational (network/API) failure. Callers defer and retry, never crash."""


def _normalize_region(region: str | None) -> str:
    if not region or len(region.strip()) != 2 or not region.strip().isalpha():
        raise ValueError("a two-letter region code is required (e.g. --region SG)")
    return region.strip().upper()


def request_guest_key(region: str, *, api_base: str, timeout_sec: float = _DEFAULT_TIMEOUT_SEC):
    """POST the guests endpoint and return the validated response dict. Rejects a malformed key
    (whitespace/control chars) rather than storing something that would clobber the secret file."""
    url = api_base.rstrip("/") + _GUESTS_PATH
    body = json.dumps({"country": region}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ProvisionError(f"guests API returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProvisionError(f"guests API unreachable: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise ProvisionError("guests API response is not an object")
    api_key = str(payload.get("api_key") or "").strip()
    if not api_key:
        raise ProvisionError("guests API response missing api_key")
    if any(ch.isspace() for ch in api_key) or not api_key.isprintable():
        raise ProvisionError("guests API returned a malformed api_key")
    return {**payload, "api_key": api_key}


def ensure(region: str | None, *, api_base: str, force: bool = False) -> dict:
    """Ensure a guest key exists. Returns a status dict; the key value is never in it."""
    if not force and secrets.read_carousell_ai_api_key() is not None:
        return {"status": "ok", "provisioned": False, "source": "store"}
    try:
        resolved = _normalize_region(region)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}
    try:
        payload = request_guest_key(resolved, api_base=api_base)
    except ProvisionError as exc:
        return {"status": "error", "error": str(exc), "defer": True}
    secrets.write_carousell_ai_api_key(payload["api_key"])
    return {
        "status": "ok",
        "provisioned": True,
        "forced": force,
        "user_id": str(payload.get("user_id") or ""),
        "country": payload.get("country") or resolved,
    }


def reprovision(region: str | None, *, api_base: str) -> dict:
    return ensure(region, api_base=api_base, force=True)
