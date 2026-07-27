"""CLI verbs that talk to a running daemon: enqueue a pass, follow it, write attended config.

`pass run` posts to the daemon's control route rather than writing selly.db directly — one writer
per store holds at the process level too. `harness config --attended` sets up an attended Claude
Code session against the same daemon MCP server (same tools, same state) as headless passes: the
.mcp.json, the slash commands, and a CLAUDE.md. Both read the attended token from the config-dir
secret; the daemon must be running.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from selly_agent import config, paths, secrets, skills

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


def _skills_dir() -> Path:
    """Where a command body should point a reader at the skill files.

    Through the `current` symlink when there is one, so an update swaps the content underneath a
    command that was written months ago. Falling back to the package's own location covers a
    checkout with no provisioned layout.
    """
    via_current = paths.current() / "src" / "selly_agent" / "skills"
    if paths.current().is_symlink() and via_current.is_dir():
        return via_current
    return skills.SKILLS_DIR


_CLAUDE_MD = """# selly-agent

This session is connected to the running selly-agent daemon over MCP. Its tools are the only way
to read or change anything the agent owns — there is no state on disk for you to edit.

## At the start of a session

Call `get_catchup` and tell the seller what is waiting on them, briefly. If nothing is, say so in
one line and move on — don't make a ceremony of it.

## Commands

`/sell` list something · `/catchup` what needs you · `/selly` status and settings ·
`/pause` and `/resume` stop and start the agent acting.

## How the agent behaves

The rulebooks the headless passes run under are readable here, and this session should follow the
same ones when it acts on the seller's behalf:

{skill_lines}
"""

_SKILL_BLURBS = (
    ("selly-conventions", "the house rules — tools only, secrets stay dark, escalate when unsure"),
    ("voice-and-style", "how outbound messages read, and the trust boundary around them"),
    ("seller-comms", "how to frame a decision the seller has to make"),
    ("listing-flow", "photos in, live listing out"),
)


def _claude_md(skills_dir: Path) -> str:
    lines = "\n".join(f"- `{skills_dir / f'{name}.md'}` — {blurb}" for name, blurb in _SKILL_BLURBS)
    return _CLAUDE_MD.format(skill_lines=lines)


def harness_config(args) -> int:
    """`selly-agent harness config --attended [--dir DIR]` — write the attended session's config:
    a .mcp.json pointed at the daemon's MCP server with the attended token, the slash commands,
    and a CLAUDE.md.

    No .claude/settings.json: an attended session is the seller's own, and its permissions are
    theirs to set. Command bodies reference the skill files by path rather than inlining them, so
    an update changes what they say without the files being rewritten.
    """
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
    written = [dest / ".mcp.json"]
    written[0].write_text(json.dumps(mcp, indent=2, sort_keys=True) + "\n")

    skills_dir = _skills_dir()
    commands_dir = dest / ".claude" / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    for name in skills.available_commands():
        target = commands_dir / f"{name}.md"
        body = skills.command_path(name).read_text().replace("{SKILLS_DIR}", str(skills_dir))
        target.write_text(body)
        written.append(target)

    claude_md = dest / "CLAUDE.md"
    claude_md.write_text(_claude_md(skills_dir))
    written.append(claude_md)

    for path in written:
        print(f"wrote {path}")
    return 0


def provision(args) -> int:
    """`selly-agent provision carousell-ai [--region XX]` — obtain the guest API key."""
    from selly_agent.rail import provision as rail_provision

    cfg = config.load()
    status = rail_provision.ensure(args.region or None, api_base=cfg.carousell_ai_api_base)
    print(json.dumps(status))
    return 0 if status.get("status") == "ok" else 3
