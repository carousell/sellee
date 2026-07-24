"""CLI verbs that talk to a running daemon: enqueue a pass, follow it, write attended config.

`pass run` posts to the daemon's control route rather than writing selly.db directly — one writer
per store holds at the process level too. `harness config --attended` drops a .mcp.json pointing an
attended Claude Code session at the same daemon MCP server (same tools, same state) as headless
passes. Both read the attended token from the config-dir secret; the daemon must be running.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from selly_agent import config, secrets

_LOCALHOST_ORIGIN = "http://127.0.0.1"


def _base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


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


def _require_token() -> str | None:
    token = secrets.read_mcp_token()
    if not token:
        print(
            "selly-agent: no MCP token found — start the daemon first (selly-agent daemon run)",
            file=sys.stderr,
        )
    return token


def run(args) -> int:
    """`selly-agent pass run <type> --item <id> [--follow]`."""
    token = _require_token()
    if not token:
        return 1
    port = config.load().http_port
    payload = {}
    if getattr(args, "item", None):
        payload["item_id"] = args.item
    try:
        resp = _post(
            f"{_base_url(port)}/control/enqueue-pass",
            token,
            {"type": args.pass_type, "payload": payload},
        )
    except (urllib.error.URLError, OSError) as exc:
        print(f"selly-agent: could not reach the daemon: {exc}", file=sys.stderr)
        return 1
    pass_id = resp["pass_id"]
    print(pass_id)
    if args.follow:
        _follow(port, token, pass_id)
    return 0


def _follow(port: int, token: str, pass_id: str) -> None:
    after = 0
    url = f"{_base_url(port)}/events.json"
    while True:
        try:
            data = _get(f"{url}?token={token}&pass={pass_id}&after_seq={after}")
        except (urllib.error.URLError, OSError):
            time.sleep(1.0)
            continue
        for event in data["events"]:
            print(f"{event['kind']:<16} {json.dumps(event['payload'], sort_keys=True)}", flush=True)
            after = event["seq"]
            if event["kind"] == "pass.end":
                return
        time.sleep(1.0)


def harness_config(args) -> int:
    """`selly-agent harness config --attended [--dir DIR]` — write .mcp.json for an attended
    session pointed at the daemon's MCP server with the attended token."""
    token = _require_token()
    if not token:
        return 1
    port = config.load().http_port
    dest = Path(args.dir) if args.dir else Path.cwd()
    dest.mkdir(parents=True, exist_ok=True)
    mcp = {
        "mcpServers": {
            "selly": {
                "type": "http",
                "url": f"{_base_url(port)}/mcp",
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }
    target = dest / ".mcp.json"
    target.write_text(json.dumps(mcp, indent=2, sort_keys=True) + "\n")
    print(f"wrote {target}")
    return 0


def provision(args) -> int:
    """`selly-agent provision carousell-ai [--region XX]` — obtain the guest API key."""
    from selly_agent.rail import provision as rail_provision

    cfg = config.load()
    status = rail_provision.ensure(args.region or None, api_base=cfg.carousell_ai_api_base)
    print(json.dumps(status))
    return 0 if status.get("status") == "ok" else 3
