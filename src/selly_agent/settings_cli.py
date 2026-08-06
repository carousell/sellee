"""`selly-agent settings list|set|approve|cancel|undo` — the attended settings door.

Harness-independent: these verbs talk to the running daemon over its control routes (attended
bearer from the config-dir secret), never writing selly.db directly. The door is the same trust as
the channel buttons — an authenticated surface, a deterministic parse, a deterministic apply — so an
unbound, attended-only install can still approve a held change (the id comes from `settings list`).

`set` skips the approval round-trip that `propose_setting_change` goes through, and only that:
the gate is there to keep the *model* from changing things unasked, and someone typing here has
already given the signal it waits for. The value is JSON, so a list stays a list; a bare word is
taken as text for the settings that hold text.
"""

from __future__ import annotations

import sys

from selly_agent import config, control
from selly_agent.installer import checks


def run(args) -> int:
    token = control.require_token()
    if not token:
        return 1
    port = config.load().http_port
    if args.settings_command == "list":
        return _list(port, token)
    if args.settings_command == "set":
        return set_setting(port, token, args.key, args.value)
    return _decide(port, token, args.settings_command, args.change_id)


def set_setting(port: int, token: str, key: str, value: str) -> int:
    """Apply one setting through the daemon. Shared with the installer, which sets the
    marketplaces the seller opted into through this same door rather than writing them itself."""
    try:
        status, result = control.post(
            port, token, "/control/settings-set", {"key": key, "value": value}
        )
    except control.DaemonUnreachable as exc:
        print(f"selly-agent: could not reach the daemon: {exc}", file=sys.stderr)
        return 1
    if status != 200:
        print(f"selly-agent: {result.get('error', 'could not set that')}", file=sys.stderr)
        return 1
    print(result.get("message", result.get("status", "done")))
    return 0


def _list(port: int, token: str) -> int:
    try:
        status, data = control.get(port, token, "/control/settings-list")
    except control.DaemonUnreachable as exc:
        print(f"selly-agent: could not reach the daemon: {exc}", file=sys.stderr)
        return 1
    if status != 200:
        print(f"selly-agent: {data.get('error', f'HTTP {status}')}", file=sys.stderr)
        return 1
    pending = data.get("pending", [])
    if pending:
        print("Pending changes (approve/cancel by id):")
        for p in pending:
            print(
                f"  {p['change_id']}  {p['label']}: {p['current']} {checks.arrow()} {p['proposed']}"
            )
    else:
        print("No pending changes.")
    print("\nSettings:")
    for s in data.get("settings", []):
        gate = " (needs approval)" if s["requires_approval"] else ""
        print(f"  {s['key']}: {s['rendered']}{gate}")
    return 0


def _decide(port: int, token: str, action: str, change_id: str) -> int:
    try:
        status, result = control.post(
            port, token, "/control/settings-decide", {"action": action, "change_id": change_id}
        )
    except control.DaemonUnreachable as exc:
        print(f"selly-agent: could not reach the daemon: {exc}", file=sys.stderr)
        return 1
    if status != 200:
        print(f"selly-agent: {result.get('error', 'that was refused')}", file=sys.stderr)
        return 1
    print(result.get("message", result.get("status", "done")))
    return 0 if result.get("status") in ("applied", "cancelled", "undone") else 1
