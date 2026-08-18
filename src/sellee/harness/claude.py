"""The Claude Code emitter: `claude -p` argv + the workspace's .mcp.json and .claude/settings.json.

Pure functions of a PassSpec. The permission posture is no-Bash by construction: --strict-mcp-config
with only our server, and --allowedTools listing exactly the pass's rules — the tier's
mcp__<server>__* names, the web tools when granted, a path-scoped Read rule per granted media file
(last, because the flag greedily consumes what follows). stream-json output requires --verbose when
used with -p (verified against the installed CLI). Each renderer has a parse-back round-trip
validator so a malformed artifact is caught before a pass ever spawns.
"""

from __future__ import annotations

import json

from sellee.harness.model import PassSpec


def mcp_config(spec: PassSpec) -> dict:
    """Every MCP server the pass may reach: our daemon over HTTP, and — for a pass that drives the
    browser — its own Playwright server over stdio.

    The browser server is spawned by the harness, so it lives inside the pass's process group and
    dies with it; the daemon's reaper story is unchanged. Because `--strict-mcp-config` is in force,
    a server absent from here is unreachable, which is what keeps browser authority per-pass-type
    rather than ambient.
    """
    servers = {
        spec.server_name: {
            "type": "http",
            "url": spec.mcp_endpoint,
            "headers": {"Authorization": f"Bearer {spec.mcp_token}"},
        }
    }
    if spec.browser_server is not None:
        servers[spec.browser_server.name] = {
            "type": "stdio",
            "command": spec.browser_server.command,
            "args": list(spec.browser_server.args),
        }
    return {"mcpServers": servers}


def browser_tool_rules(spec: PassSpec) -> tuple:
    if spec.browser_server is None:
        return ()
    name = spec.browser_server.name
    return tuple(f"mcp__{name}__{tool}" for tool in spec.browser_server.tools)


# The harness's own web-research tools. Part of the deny list that moves: a pass whose skills tell
# it to price against comps needs them, and every other pass must not have them.
WEB_TOOLS = ("WebSearch", "WebFetch")
# Never available to any pass, at any tier — the no-Bash posture and the write/exec escape vectors.
_ALWAYS_DENIED = ("Bash", "Edit", "Write", "NotebookEdit")
READ_TOOL = "Read"


def read_rules(spec: PassSpec) -> tuple:
    """Path-scoped Read permission rules for the spec's granted files, one per file.

    `Read(//abs/path)` is the harness's rule syntax: gitignore-style, `//` anchoring at the
    filesystem root (a single `/` would anchor at the settings file). An exact path grants exactly
    one file — how a pass gets eyes on a claimed photo without any wider file access.
    """
    return tuple(f"{READ_TOOL}(/{path})" for path in spec.readable_paths)


def allowed_tools(spec: PassSpec) -> tuple:
    """Every tool rule the pass may use: its tier's MCP tools, the browser diet when it drives the
    browser, web research when its pass type asked for it, and a path-scoped Read rule per granted
    media file. Read rules stay last — the flag greedily consumes what follows."""
    return (
        tuple(spec.allowed_tools)
        + browser_tool_rules(spec)
        + (WEB_TOOLS if spec.web_tools else ())
        + read_rules(spec)
    )


def denied_tools(spec: PassSpec) -> tuple:
    # A deny beats any allow, however specific — so bare Read may appear here only when nothing is
    # granted. With grants present, everything outside them still fails: a headless pass cannot be
    # prompted, and an unmatched tool call is rejected by default.
    denied = _ALWAYS_DENIED + (() if spec.web_tools else WEB_TOOLS)
    if not spec.readable_paths:
        denied += (READ_TOOL,)
    return denied


def settings_json(spec: PassSpec) -> dict:
    """The workspace permission posture: allow exactly the tier's tools, deny the escape vectors.
    The argv --allowedTools is the real enforcement; this makes the posture legible on disk and
    covers attended sessions that read settings rather than argv."""
    return {
        "permissions": {
            "allow": list(allowed_tools(spec)),
            "deny": list(denied_tools(spec)),
        }
    }


def render_workspace(spec: PassSpec) -> dict:
    """The workspace files as a {relative_path: text} map. The runner writes these into an empty
    per-pass directory whose only contents are these files (nothing to escape to)."""
    files = {
        ".mcp.json": json.dumps(mcp_config(spec), indent=2, sort_keys=True) + "\n",
        ".claude/settings.json": json.dumps(settings_json(spec), indent=2, sort_keys=True) + "\n",
    }
    _validate_workspace_round_trip(spec, files)
    return files


def pass_argv(spec: PassSpec, claude_bin: str = "claude") -> list:
    """The full `claude -p` argv. --allowedTools is last (it greedily consumes following args)."""
    argv = [claude_bin, "-p", spec.prompt]
    if spec.append_system_prompt:
        argv += ["--append-system-prompt", spec.append_system_prompt]
    argv += ["--strict-mcp-config", "--mcp-config", json.dumps(mcp_config(spec), sort_keys=True)]
    if spec.model:
        argv += ["--model", spec.model]
    if spec.max_turns is not None:
        argv += ["--max-turns", str(spec.max_turns)]
    if spec.permission_mode:
        argv += ["--permission-mode", spec.permission_mode]
    if spec.output_format:
        argv += ["--output-format", spec.output_format]
    if spec.output_format == "stream-json":
        # -p with stream-json output requires --verbose, or the CLI refuses to start.
        argv += ["--verbose"]
    allowed = allowed_tools(spec)
    if allowed:
        argv += ["--allowedTools", *allowed]
    _validate_argv_round_trip(spec, argv)
    return argv


# --- round-trip validators (INV-35): parse our own output back and assert it matches ----------


def _validate_servers(spec: PassSpec, parsed: dict) -> None:
    """Every rendered server matches the spec, and no server we did not ask for is present.

    The last clause is the load-bearing one: with --strict-mcp-config the rendered set IS the pass's
    reachable surface, so an extra entry would be an authority grant that no pass type asked for.
    """
    servers = parsed["mcpServers"]
    server = servers[spec.server_name]
    if server["url"] != spec.mcp_endpoint:
        raise ValueError("rendered mcp config url does not match the spec")
    if server["headers"]["Authorization"] != f"Bearer {spec.mcp_token}":
        raise ValueError("rendered mcp config authorization does not match the spec")
    expected = {spec.server_name}
    if spec.browser_server is not None:
        expected.add(spec.browser_server.name)
        browser = servers[spec.browser_server.name]
        if browser["type"] != "stdio":
            raise ValueError("the browser server must be rendered as a stdio server")
        if browser["command"] != spec.browser_server.command:
            raise ValueError("rendered browser server command does not match the spec")
        if browser["args"] != list(spec.browser_server.args):
            raise ValueError("rendered browser server args do not match the spec")
    if set(servers) != expected:
        raise ValueError("rendered mcp config carries a server the spec did not ask for")


def _validate_workspace_round_trip(spec: PassSpec, files: dict) -> None:
    _validate_servers(spec, json.loads(files[".mcp.json"]))
    settings = json.loads(files[".claude/settings.json"])
    if settings["permissions"]["allow"] != list(allowed_tools(spec)):
        raise ValueError("rendered settings.json allow-list does not match the spec")
    denied = settings["permissions"]["deny"]
    if any(name in denied for name in allowed_tools(spec)):
        raise ValueError("rendered settings.json both allows and denies a tool")
    if not spec.web_tools and not all(name in denied for name in WEB_TOOLS):
        raise ValueError("a pass without web tools must deny them explicitly")
    # The diet is the point of granting a browser server at all: reaching the server is necessary
    # but the allow-list is what bounds which of its tools the pass can call.
    for rule in browser_tool_rules(spec):
        if rule not in settings["permissions"]["allow"]:
            raise ValueError("a granted browser tool is missing from the allow-list")
    if spec.readable_paths:
        # A bare Read deny would override every path-scoped allow (deny beats allow, regardless
        # of specificity) — granted files must not be revoked by the same artifact.
        if READ_TOOL in denied:
            raise ValueError("a pass with granted media must not deny Read outright")
        for rule in read_rules(spec):
            if rule not in settings["permissions"]["allow"]:
                raise ValueError("a granted media path is missing from the allow-list")
    elif READ_TOOL not in denied:
        raise ValueError("a pass with no granted media must deny Read explicitly")


def _validate_argv_round_trip(spec: PassSpec, argv: list) -> None:
    if argv[1] != "-p" or argv[2] != spec.prompt:
        raise ValueError("argv does not lead with -p <prompt>")
    if "--strict-mcp-config" not in argv:
        raise ValueError("argv is missing --strict-mcp-config")
    _validate_servers(spec, json.loads(argv[argv.index("--mcp-config") + 1]))
    if spec.output_format == "stream-json" and "--verbose" not in argv:
        raise ValueError("stream-json output requires --verbose")
    allowed = allowed_tools(spec)
    if allowed:
        idx = argv.index("--allowedTools")
        if list(argv[idx + 1 :]) != list(allowed):
            raise ValueError("--allowedTools must be last and list exactly the pass's tools")
    if spec.append_system_prompt and spec.append_system_prompt not in argv:
        raise ValueError("argv is missing the composed system prompt")
