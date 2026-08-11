"""`selly-agent connect <telegram|marketplace>` — the optional connections, over control routes.

Two shapes behind one verb. Telegram binds a bot: a credential exchange, described below.
A marketplace is a sign-in the seller does themselves in the agent's own Chrome — the daemon
opens the page, the probe reads back whether it worked, and nothing about the session is stored.

The Telegram token is a long-lived credential, so it never touches argv: it is read from stdin
and POSTed to the running daemon, which validates it, stores it 0600, and mints a bind nonce. The
CLI prints the deep link as a terminal QR plus the link itself (scan or open it on the phone that
has Telegram) and polls until the chat binds.
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
import sys
import time

from selly_agent import config, control, deployment, qr
from selly_agent.browser import foreground

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
    token = control.require_token()
    if not token:
        return 3
    port = config.load().http_port
    if args.connect_command == "telegram":
        if getattr(args, "status", False):
            return _print_status(port, token)
        return bind_flow(port, token, timeout=getattr(args, "timeout", None))
    return market_flow(port, token, args.connect_command)


# --- marketplaces ---------------------------------------------------------------------------
#
# Signing in to a marketplace happens in the agent's own Chrome, on a page the daemon opens, and
# nothing about it is stored: the cookies in that profile are the state, and the probe re-derives
# the answer every time it is asked. So this verb is re-runnable forever and has nothing to undo.

_MARKET_STATE_MESSAGES = {
    "logged_in": "✅ Signed in to {name}.",
    "unknown": "I can't tell whether you're signed in to {name} — I'll confirm the first time I "
    "list something there.",
    "logged_out": "I still see a login screen on {name}. Sign in on that tab and re-run this "
    "when you're done — I'll keep checking in the background too.",
}


def market_flow(port: int, mcp_token: str, market: str, *, interactive: bool | None = None) -> int:
    """Open a marketplace for sign-in and report what the login probe then sees.

    Exit codes mirror the Telegram flow: 0 signed in · 1 not yet (re-runnable) · 3 the daemon or
    the browser could not do it. Shared with the installer's marketplace step, so both paths ask
    in exactly one way.
    """
    if interactive is None:
        interactive = sys.stdin.isatty()

    try:
        status, body = control.post(port, mcp_token, "/control/connect-market", {"market": market})
    except control.DaemonUnreachable as exc:
        print(f"selly-agent: could not reach the daemon: {exc}", file=sys.stderr)
        return 3
    if status == 409:
        print(
            f"selly-agent: {body.get('detail', 'the browser is busy')} — "
            "try again in a minute or two.",
            file=sys.stderr,
        )
        return 3
    if status == 503:
        print(f"selly-agent: {body.get('detail', 'the browser is unavailable')}", file=sys.stderr)
        return 3
    if status != 200:
        print(
            f"selly-agent: {body.get('error', 'could not open that marketplace')}", file=sys.stderr
        )
        return 3

    name = _display_name(market)
    state = body.get("state")
    if state == "logged_in":
        print(_MARKET_STATE_MESSAGES["logged_in"].format(name=name))
        return 0

    print(f"Opened {name} in my Chrome window — sign in there. I never sign in for you.")
    print(f"  {body.get('url', '')}")
    _surface_window(body)
    if interactive:
        try:
            input("Press Enter once you've signed in (or Ctrl-C to skip)… ")
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return 1
        state = _probe_market(port, mcp_token, market)

    print(_MARKET_STATE_MESSAGES.get(state or "unknown", "").format(name=name))
    return 0 if state == "logged_in" else 1


def _surface_window(body: dict) -> None:
    """Bring the agent's Chrome window in front of the seller, or say where to look instead.

    The raise happens here — the seller's own frontmost terminal, where macOS honors activation —
    not in the daemon. It runs only when the daemon says the seller wants it (the `raise_window`
    field carries their `raise_browser` setting; absent means the default, raise). Success is
    silent: the window arriving in front of them is its own message. Every way it cannot happen —
    a container's Chrome on another machine, an OS the raise doesn't support, background mode, a
    raise that failed — prints a hint and nothing more; finding the window is recoverable, a broken
    sign-in flow is not. Checked in that order, so the setting is only ever named where it is
    actually the reason.
    """
    if deployment.is_container():
        print(
            "  That window is on your own computer — the Chrome you started with "
            "start-chrome.sh (start-chrome.ps1 on Windows)."
        )
        return
    if not foreground.is_supported():
        print(
            "  Look for a separate Chrome window (mine, not your usual one) — I can't "
            "bring it to the front on this operating system."
        )
        return
    if not body.get("raise_window", True):
        print(
            "  My window stays in the background (your setting) — look for a separate "
            "Chrome window; /selly changes this."
        )
        return
    if not foreground.raise_window(config.load().chrome_cdp_port):
        print(
            "  Can't spot it? Look for a separate Chrome window (mine, not your usual "
            "one) — check the Dock if you minimized it."
        )


def _probe_market(port: int, mcp_token: str, market: str):
    try:
        status, answer = control.get(port, mcp_token, "/control/market-login", {"market": market})
    except control.DaemonUnreachable:
        return "unknown"
    # A refusal (the browser went away, a pass took the tab) is not an answer about the login,
    # and "unknown" is the honest reading — never "logged_out", which would alarm for nothing.
    return answer.get("state") if status == 200 else "unknown"


def _display_name(market: str) -> str:
    from selly_agent import marketplaces

    return marketplaces.display_name(market)


def bind_flow(
    port: int,
    mcp_token: str,
    *,
    timeout: int | None = None,
    interactive: bool | None = None,
) -> int:
    """Run the full connect-telegram UX and return the process exit code.

    Guidance → token read → POST /control/connect-telegram → print identity + QR + start_url +
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

    try:
        status, body = control.post(
            port, mcp_token, "/control/connect-telegram", {"token": bot_token}, timeout=60
        )
    except control.DaemonUnreachable as exc:
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
    """Show the bot identity, a scannable QR of the deep link, and the link itself.

    Wording is phone-oriented ("the phone that has Telegram"), not "tap the link" — the operator
    is often at a desktop with no Telegram, so the link has to travel to the phone, and the QR is
    the shortest way across. The link stays printed (copy/paste + accessibility, and the fallback
    when the terminal render won't scan). Rendering is local — an online QR service would ship
    the single-use nonce off the machine.
    """
    print(f"Bot @{bot_username} validated.\n")
    print(qr.render_terminal(start_url))
    print("Scan the code with the phone that has Telegram — or open this link on it —")
    print("then tap Start:")
    print(f"  {start_url}")
    print("  On a desktop with no Telegram? Send the link to your phone (message it to")
    print("  yourself) and open it there — don't just type /start, the link carries a")
    print("  one-time code that binds your chat.")
    print(f"\nWaiting for you to start the bot (up to {timeout}s)...")


def _await_bind(port: int, token: str, *, timeout: int, interactive: bool) -> int:
    deadline = time.monotonic() + timeout
    next_notice = timeout - _REMAINING_NOTICE_SEC
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            _, status = control.get(port, token, "/control/channel-status")
        except control.DaemonUnreachable:
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
        code, status = control.get(port, token, "/control/channel-status")
    except control.DaemonUnreachable as exc:
        print(f"selly-agent: could not reach the daemon: {exc}", file=sys.stderr)
        return 3
    if code != 200:
        print(f"selly-agent: {status.get('error', f'HTTP {code}')}", file=sys.stderr)
        return 3
    if status.get("bound"):
        print(f"bound to @{status.get('bot_username')}")
        return 0
    if status.get("awaiting_bind"):
        print(f"awaiting /start for @{status.get('bot_username')}")
        return 1
    print("not connected")
    return 1
