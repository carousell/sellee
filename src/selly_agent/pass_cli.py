"""CLI verbs that talk to a running daemon: enqueue a pass, follow it, write attended config.

`pass run` posts to the daemon's control route rather than writing selly.db directly — one writer
per store holds at the process level too. `harness config --attended` sets up an attended Claude
Code session against the same daemon MCP server (same tools, same state) as headless passes: the
.mcp.json, the slash commands, and a CLAUDE.md. `chat` is the door a seller actually uses: the same
config, written to a fixed place, then Claude Code launched in it. All three read the attended
token from the config-dir secret; the daemon must be running.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from selly_agent import config, control, paths, pointer, skills, spawn

# The seam tests replace to observe the launch instead of becoming a Claude Code session.
_exec = spawn.become


def run(args) -> int:
    """`selly-agent pass run <type> --item <id> [--market <id>] [--follow]`."""
    token = control.require_token()
    if not token:
        return 1
    port = config.load().http_port
    payload = {}
    if getattr(args, "item", None):
        payload["item_id"] = args.item
    if getattr(args, "market", None):
        payload["market"] = args.market
    try:
        status, resp = control.post(
            port, token, "/control/enqueue-pass", {"type": args.pass_type, "payload": payload}
        )
    except control.DaemonUnreachable as exc:
        print(f"selly-agent: could not reach the daemon: {exc}", file=sys.stderr)
        return 1
    if status != 200:
        print(f"selly-agent: {resp.get('error', 'could not enqueue that pass')}", file=sys.stderr)
        return 1
    pass_id = resp["pass_id"]
    print(pass_id)
    if args.follow:
        _follow(port, token, pass_id)
    return 0


def _follow(port: int, token: str, pass_id: str) -> None:
    after = 0
    while True:
        try:
            status, data = control.get(
                port, token, "/events.json", {"pass": pass_id, "after_seq": after}
            )
        except control.DaemonUnreachable:
            time.sleep(1.0)
            continue
        if status != 200:
            # The daemon answered and said no — a stale token, a bad pass id. Unlike a daemon
            # that is momentarily down, retrying will get the same refusal forever.
            print(f"selly-agent: {data.get('error', f'HTTP {status}')}", file=sys.stderr)
            return
        for event in data["events"]:
            print(f"{event['kind']:<16} {json.dumps(event['payload'], sort_keys=True)}", flush=True)
            after = event["seq"]
            if event["kind"] == "pass.end":
                return
        time.sleep(1.0)


def _skills_dir() -> Path:
    """Where a command body should point a reader at the skill files.

    Through the `current` pointer when there is one, so an update swaps the content underneath a
    command that was written months ago. Falling back to the package's own location covers a
    checkout with no provisioned layout.
    """
    via_current = paths.current() / "src" / "selly_agent" / "skills"
    if pointer.is_pointer(paths.current()) and via_current.is_dir():
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
    ("listing-flow-carousell", "the same, filled into Carousell's own composer in the browser"),
    ("buyer-conversation", "how a buyer's message is classified, answered, and negotiated"),
    ("scam-guard", "the never-engage rule, and what to do with a flagged thread"),
)


def _claude_md(skills_dir: Path) -> str:
    lines = "\n".join(f"- `{skills_dir / f'{name}.md'}` — {blurb}" for name, blurb in _SKILL_BLURBS)
    return _CLAUDE_MD.format(skill_lines=lines)


def harness_config(directory=None, *, quiet: bool = False) -> int:
    """`selly-agent harness config --attended [--dir DIR]` — write the attended session's config:
    a .mcp.json pointed at the daemon's MCP server with the attended token, the slash commands,
    and a CLAUDE.md.

    Takes the destination directly rather than a parsed-args object, so the installer can
    generate the same workspace at its own fixed location without fabricating one. `quiet` is for
    callers this is a step of rather than the point of — listing seven paths is the useful answer
    to `harness config`, and preamble in front of a session someone asked to start.

    No .claude/settings.json: an attended session is the seller's own, and its permissions are
    theirs to set — and this rewrites only the three things it generates, so a permission granted
    in a past session survives. Command bodies reference the skill files by path rather than
    inlining them, so an update changes what they say without the files being rewritten.
    """
    token = control.require_token()
    if not token:
        return 1
    port = config.load().http_port
    dest = Path(directory) if directory else Path.cwd()
    dest.mkdir(parents=True, exist_ok=True)
    mcp = {
        "mcpServers": {
            "selly": {
                "type": "http",
                "url": f"{control.base_url(port)}/mcp",
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
        body = (
            skills.command_path(name)
            .read_text(encoding="utf-8")
            .replace("{SKILLS_DIR}", str(skills_dir))
        )
        target.write_text(body, encoding="utf-8")
        written.append(target)

    claude_md = dest / "CLAUDE.md"
    claude_md.write_text(_claude_md(skills_dir), encoding="utf-8")
    written.append(claude_md)

    if not quiet:
        for path in written:
            print(f"wrote {path}")
    return 0


def attended_dir() -> Path:
    """Where the attended workspace lives — a fixed, documented place rather than wherever the
    terminal happened to be, because it is what the installer provisions and `chat` launches."""
    return paths.data_root() / "attended"


def chat(args=None) -> int:
    """`selly-agent chat` — regenerate the attended workspace, then become a Claude Code session.

    Regenerated per launch rather than trusted from install time: the .mcp.json carries the
    daemon's port and the attended token, and an update or a rotated token would otherwise leave a
    session pointed at nothing.

    The daemon is checked before the workspace is written, because every tool this session has is
    served by it — launched against a stopped daemon, the session comes up with no way to read or
    change anything and no obvious reason why.
    """
    token = control.require_token()
    if not token:
        return 1
    port = config.load().http_port
    try:
        status, body = control.get(port, token, "/control/channel-status")
    except control.DaemonUnreachable:
        print(
            "selly-agent: the daemon is not running — start it with `selly-agent daemon start`",
            file=sys.stderr,
        )
        return 1
    if status != 200:
        print(f"selly-agent: {body.get('error', f'HTTP {status}')}", file=sys.stderr)
        return 1

    dest = attended_dir()
    if harness_config(dest, quiet=True) != 0:
        return 1

    from selly_agent import passes

    binary = passes.resolve_claude_bin(config.load())
    if binary is None:
        print(
            "selly-agent: claude binary not found — set claude_bin in config or add it to PATH",
            file=sys.stderr,
        )
        return 1

    # exec inherits the file descriptors but not Python's own buffers, so anything still sitting
    # in them is lost rather than printed — silently, and only when stdout is a pipe.
    sys.stdout.flush()
    sys.stderr.flush()
    # Where the platform allows it this process is replaced, so signals and the tty behave exactly
    # as if the seller had run `claude` themselves; where it does not, the session is run to
    # completion and its status returned.
    os.chdir(dest)
    return _exec([binary])


def provision(args) -> int:
    """`selly-agent provision carousell-ai [--region XX]` — obtain the guest API key."""
    from selly_agent.rail import provision as rail_provision

    cfg = config.load()
    status = rail_provision.ensure(args.region or None, api_base=cfg.carousell_ai_api_base)
    print(json.dumps(status))
    return 0 if status.get("status") == "ok" else 3
