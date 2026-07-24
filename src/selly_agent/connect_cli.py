"""`selly-agent connect telegram` — bind a Telegram bot over the daemon's control route.

The token is a long-lived credential, so it never touches argv: it is read from stdin and POSTed to
the running daemon, which validates it, stores it 0600, and mints a bind nonce. The CLI prints the
deep link (open it on the phone that has Telegram) and polls channel-status until the chat binds.
Exit codes: 0 bound · 1 awaiting /start (timed out, re-runnable) · 2 bad token · 3 daemon/API error.

Interactive vs piped:

- Interactive (stdin is a TTY): print short BotFather guidance, then read the token with
  ``getpass`` — prompted and not echoed, so a credential never lands in the terminal scrollback.
- Piped / scripted / installer with a pipe (stdin is not a TTY): read one ``readline()`` with no
  prompt and no guidance, so a token can be fed in non-interactively.

The bind flow (guidance → token read → POST → print identity + start_url + phone-delivery
guidance → poll) lives in :func:`bind_flow` so the installer's inline "want your agent on your
phone?" offer shares one implementation of the UX.
"""

from __future__ import annotations

import getpass
import json
import sys
import time
import urllib.error
import urllib.request

from selly_agent import config, secrets

_LOCALHOST_ORIGIN = "http://127.0.0.1"
_POLL_INTERVAL_SEC = 1.0
# Getting the deep link onto a phone can take a while for a desktop operator, so the interactive
# default is generous; the piped/scripted default stays tight (a script isn't waiting on a human).
_INTERACTIVE_TIMEOUT_SEC = 300
_PIPED_TIMEOUT_SEC = 120
# While polling interactively, remind the operator of the remaining wait at this cadence.
_REMAINING_NOTICE_SEC = 30

_BOTFATHER_GUIDANCE = (
    "To connect Telegram you need a bot token from BotFather:\n"
    "  1. Open Telegram and message @BotFather\n"
    "  2. Send /newbot and follow the prompts (a name, then a username)\n"
    "  3. Copy the HTTP API token it replies with (looks like 123456789:AA...)\n"
)


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


def _read_token(interactive: bool) -> str:
    """Read the BotFather token. Interactive: a non-echoed getpass prompt (a credential must stay
    off the scrollback). Piped: one readline, no prompt."""
    if interactive:
        try:
            return getpass.getpass("Paste your BotFather bot token: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)  # close the dangling prompt line
            return ""
    return sys.stdin.readline().strip()


def run(args) -> int:
    token = _require_token()
    if not token:
        return 3
    port = config.load().http_port
    if getattr(args, "status", False):
        return _print_status(port, token)
    return bind_flow(port, token, timeout=getattr(args, "timeout", None))


def bind_flow(
    port: int,
    mcp_token: str,
    *,
    timeout: int | None = None,
    interactive: bool | None = None,
) -> int:
    """Run the full connect-telegram UX and return the process exit code.

    Guidance → token read → POST /control/connect-telegram → print identity + start_url +
    phone-delivery guidance → poll channel-status until bound or timeout. Shared by the standalone
    command and the installer's inline offer; the caller owns any offer/accept/decline framing, this
    owns everything from the token to a bound chat.

    ``interactive`` defaults to whether stdin is a TTY; ``timeout`` defaults to 300s interactive /
    120s piped (a desktop operator relaying the link to a phone needs the longer window).
    """
    if interactive is None:
        interactive = sys.stdin.isatty()
    if timeout is None:
        timeout = _INTERACTIVE_TIMEOUT_SEC if interactive else _PIPED_TIMEOUT_SEC

    if interactive:
        print(_BOTFATHER_GUIDANCE)

    bot_token = _read_token(interactive)
    if not bot_token:
        if interactive:
            print("selly-agent: no token entered — nothing to connect.", file=sys.stderr)
        else:
            print("selly-agent: no token on stdin — pipe the BotFather token in", file=sys.stderr)
        return 2

    url = f"{_base_url(port)}/control/connect-telegram"
    try:
        status, body = _post(url, mcp_token, {"token": bot_token})
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

    _print_bind_prompt(body["bot_username"], body["start_url"], timeout=timeout)
    return _await_bind(port, mcp_token, timeout=timeout, interactive=interactive)


def _print_bind_prompt(bot_username: str, start_url: str, *, timeout: int) -> None:
    """Show the bot identity and the deep link the operator must open on their phone.

    Wording is phone-oriented ("open on the phone that has Telegram"), not "tap the link" — the
    operator is often at a desktop with no Telegram, so the link has to travel to the phone. The URL
    is printed prominently with one line on getting it across (no online-QR suggestion — the nonce
    is a single-use secret).
    """
    print(f"Bot @{bot_username} validated.\n")
    print("Open this link on the phone that has Telegram, then tap Start:")
    print(f"  {start_url}")
    print("  On a desktop with no Telegram? Send the link to your phone (message it to")
    print("  yourself) and open it there — don't just type /start, the link carries a")
    print("  one-time code that binds your chat.")
    print(f"\nWaiting for you to start the bot (up to {timeout}s)...")


def _await_bind(port: int, token: str, *, timeout: int, interactive: bool) -> int:
    deadline = time.monotonic() + timeout
    url = f"{_base_url(port)}/control/channel-status?token={token}"
    next_notice = timeout - _REMAINING_NOTICE_SEC
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            status = _get(url)
        except (urllib.error.URLError, OSError):
            status = {}
        if status.get("bound"):
            print(f"Connected as @{status.get('bot_username')}.")
            return 0
        if interactive and remaining <= next_notice:
            print(f"  still waiting — {int(remaining)}s left...")
            next_notice = remaining - _REMAINING_NOTICE_SEC
        time.sleep(_POLL_INTERVAL_SEC)
    print(
        "Timed out waiting for /start. Open the link on your phone, then re-run: "
        "selly-agent connect telegram",
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
