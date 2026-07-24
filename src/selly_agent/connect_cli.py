"""`selly-agent connect telegram` — bind a Telegram bot over the daemon's control route.

The token is a long-lived credential, so it never touches argv: it is read from one line of stdin
and POSTed to the running daemon, which validates it, stores it 0600, and mints a bind nonce. The
CLI prints the deep link (tap it — a bare /start won't bind) and polls channel-status until the
chat binds. Exit codes mirror the legacy bind discipline: 0 bound · 1 awaiting /start (timed out,
re-runnable) · 2 bad token · 3 daemon/API error.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

from selly_agent import config, secrets

_LOCALHOST_ORIGIN = "http://127.0.0.1"
_POLL_INTERVAL_SEC = 1.0


def _base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _require_token() -> str | None:
    token = secrets.read_mcp_token()
    if not token:
        print(
            "selly-agent: no MCP token found — start the daemon first (selly-agent daemon run)",
            file=sys.stderr,
        )
    return token


def _post(url: str, token: str, body: dict) -> tuple:
    """POST and return (status, parsed_json). A 4xx/5xx body is read too (it carries the error)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Origin": _LOCALHOST_ORIGIN,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            return exc.code, {}


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Origin": _LOCALHOST_ORIGIN})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run(args) -> int:
    token = _require_token()
    if not token:
        return 3
    port = config.load().http_port
    if getattr(args, "status", False):
        return _print_status(port, token)
    return _connect(port, token, timeout=getattr(args, "timeout", 120))


def _connect(port: int, token: str, *, timeout: int) -> int:
    bot_token = sys.stdin.readline().strip()
    if not bot_token:
        print("selly-agent: no token on stdin — pipe the BotFather token in", file=sys.stderr)
        return 2
    url = f"{_base_url(port)}/control/connect-telegram"
    try:
        status, body = _post(url, token, {"token": bot_token})
    except (urllib.error.URLError, OSError) as exc:
        print(f"selly-agent: could not reach the daemon: {exc}", file=sys.stderr)
        return 3
    if status != 200:
        kind = body.get("error", "error")
        if kind in ("bad_token_format", "unauthorized"):
            print(f"selly-agent: token rejected ({kind})", file=sys.stderr)
            return 2
        print(f"selly-agent: Telegram API error ({body.get('detail', kind)})", file=sys.stderr)
        return 3

    print(f"Bot @{body['bot_username']} validated. Open this link and tap Start:")
    print(f"  {body['start_url']}")
    print("(tap the link — a plain /start won't bind)")
    print("Waiting for you to start the bot...")
    return _await_bind(port, token, timeout=timeout)


def _await_bind(port: int, token: str, *, timeout: int) -> int:
    deadline = time.monotonic() + timeout
    url = f"{_base_url(port)}/control/channel-status?token={token}"
    while time.monotonic() < deadline:
        try:
            status = _get(url)
        except (urllib.error.URLError, OSError):
            status = {}
        if status.get("bound"):
            print(f"Connected as @{status.get('bot_username')}.")
            return 0
        time.sleep(_POLL_INTERVAL_SEC)
    print(
        "Timed out waiting for /start. Tap the link, then re-run: selly-agent connect telegram",
        file=sys.stderr,
    )
    return 1


def _print_status(port: int, token: str) -> int:
    try:
        status = _get(f"{_base_url(port)}/control/channel-status?token={token}")
    except (urllib.error.URLError, OSError) as exc:
        print(f"selly-agent: could not reach the daemon: {exc}", file=sys.stderr)
        return 3
    if status.get("bound"):
        print(f"bound to @{status.get('bot_username')}")
        return 0
    if status.get("awaiting_bind"):
        print(f"awaiting /start for @{status.get('bot_username')}")
        return 1
    print("not connected")
    return 1
