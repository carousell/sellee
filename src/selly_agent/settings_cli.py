"""`selly-agent settings list|approve|cancel|undo` — the attended settings door.

Harness-independent: these verbs talk to the running daemon over its control routes (attended
bearer from the config-dir secret), never writing selly.db directly. The door is the same trust as
the channel buttons — an authenticated surface, a deterministic parse, a deterministic apply — so an
unbound, attended-only install can still approve a held change (the id comes from `settings list`).
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from selly_agent import config, secrets

_LOCALHOST_ORIGIN = "http://127.0.0.1"


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


def _post(url: str, token: str, body: dict) -> dict:
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
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Origin": _LOCALHOST_ORIGIN})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run(args) -> int:
    token = _require_token()
    if not token:
        return 1
    port = config.load().http_port
    if args.settings_command == "list":
        return _list(port, token)
    return _decide(port, token, args.settings_command, args.change_id)


def _list(port: int, token: str) -> int:
    try:
        data = _get(f"{_base_url(port)}/control/settings-list?token={token}")
    except (urllib.error.URLError, OSError) as exc:
        print(f"selly-agent: could not reach the daemon: {exc}", file=sys.stderr)
        return 1
    pending = data.get("pending", [])
    if pending:
        print("Pending changes (approve/cancel by id):")
        for p in pending:
            print(f"  {p['change_id']}  {p['label']}: {p['current']} → {p['proposed']}")
    else:
        print("No pending changes.")
    print("\nSettings:")
    for s in data.get("settings", []):
        gate = " (needs approval)" if s["requires_approval"] else ""
        print(f"  {s['key']}: {s['rendered']}{gate}")
    return 0


def _decide(port: int, token: str, action: str, change_id: str) -> int:
    try:
        result = _post(
            f"{_base_url(port)}/control/settings-decide",
            token,
            {"action": action, "change_id": change_id},
        )
    except (urllib.error.URLError, OSError) as exc:
        print(f"selly-agent: could not reach the daemon: {exc}", file=sys.stderr)
        return 1
    print(result.get("message", result.get("status", "done")))
    return 0 if result.get("status") in ("applied", "cancelled", "undone") else 1
