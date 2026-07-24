"""The Claude Code emitter: `claude -p` argv + the workspace's .mcp.json and .claude/settings.json.

Pure functions of a PassSpec. The permission posture is no-Bash by construction: --strict-mcp-config
with only our server, and --allowedTools listing exactly the tier's mcp__<server>__* names (last,
because the flag greedily consumes what follows). stream-json output requires --verbose when used
with -p (verified against the installed CLI). Each renderer has a parse-back round-trip validator so
a malformed artifact is caught before a pass ever spawns.
"""

from __future__ import annotations

import json

from selly_agent.harness.model import PassSpec


def mcp_config(spec: PassSpec) -> dict:
    """The single MCP server the pass may reach — our daemon, over HTTP, bearer-authenticated."""
    return {
        "mcpServers": {
            spec.server_name: {
                "type": "http",
                "url": spec.mcp_endpoint,
                "headers": {"Authorization": f"Bearer {spec.mcp_token}"},
            }
        }
    }


def settings_json(spec: PassSpec) -> dict:
    """The workspace permission posture: allow exactly the tier's tools, deny the escape vectors.
    The argv --allowedTools is the real enforcement; this makes the posture legible on disk and
    covers attended sessions that read settings rather than argv."""
    return {
        "permissions": {
            "allow": list(spec.allowed_tools),
            "deny": ["Bash", "Edit", "Write", "Read", "WebFetch", "WebSearch", "NotebookEdit"],
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
    if spec.allowed_tools:
        argv += ["--allowedTools", *spec.allowed_tools]
    _validate_argv_round_trip(spec, argv)
    return argv


# --- round-trip validators (INV-35): parse our own output back and assert it matches ----------


def _validate_workspace_round_trip(spec: PassSpec, files: dict) -> None:
    parsed = json.loads(files[".mcp.json"])
    server = parsed["mcpServers"][spec.server_name]
    if server["url"] != spec.mcp_endpoint:
        raise ValueError("rendered .mcp.json url does not match the spec")
    if server["headers"]["Authorization"] != f"Bearer {spec.mcp_token}":
        raise ValueError("rendered .mcp.json authorization does not match the spec")
    settings = json.loads(files[".claude/settings.json"])
    if settings["permissions"]["allow"] != list(spec.allowed_tools):
        raise ValueError("rendered settings.json allow-list does not match the spec")


def _validate_argv_round_trip(spec: PassSpec, argv: list) -> None:
    if argv[1] != "-p" or argv[2] != spec.prompt:
        raise ValueError("argv does not lead with -p <prompt>")
    if "--strict-mcp-config" not in argv:
        raise ValueError("argv is missing --strict-mcp-config")
    cfg_json = argv[argv.index("--mcp-config") + 1]
    if json.loads(cfg_json)["mcpServers"][spec.server_name]["url"] != spec.mcp_endpoint:
        raise ValueError("argv --mcp-config does not match the spec")
    if spec.output_format == "stream-json" and "--verbose" not in argv:
        raise ValueError("stream-json output requires --verbose")
    if spec.allowed_tools:
        idx = argv.index("--allowedTools")
        if list(argv[idx + 1 :]) != list(spec.allowed_tools):
            raise ValueError("--allowedTools must be last and list exactly the tier's tools")
