"""The Codex emitter — a stub: it renders .codex/config.toml but has no spawn path in this plan.

Keeping a second real emitter honest forces the internal PassSpec to stay genuinely common. Codex
reaches our MCP server through the stdio proxy (it has no first-class HTTP MCP transport), so the
config points at `selly-agent mcp-proxy` rather than carrying the endpoint or token — the proxy
resolves those from config/secrets, keeping the token out of this file. A hand-rolled TOML writer
plus a matching minimal parser give the INV-35 round-trip without a 3.11+ tomllib dependency.

Codex runtime quirks (no --allowedTools, no hooks in exec mode) are the reason there is no spawn
path here; that parity work is gated to the cutover plan.
"""

from __future__ import annotations

from selly_agent.harness.model import PassSpec


def render_config(spec: PassSpec) -> str:
    """Render .codex/config.toml for this spec and validate it round-trips."""
    lines = [
        f'model = "{spec.model}"',
        "",
        f"[mcp_servers.{spec.server_name}]",
        'command = "selly-agent"',
        'args = ["mcp-proxy"]',
        "",
    ]
    # A browser-driving pass reaches Playwright over stdio, which Codex supports natively — so the
    # multi-server shape stays common to both emitters even though only Claude has a spawn path.
    if spec.browser_server is not None:
        args = ", ".join(f'"{arg}"' for arg in spec.browser_server.args)
        lines += [
            f"[mcp_servers.{spec.browser_server.name}]",
            f'command = "{spec.browser_server.command}"',
            f"args = [{args}]",
            "",
        ]
    text = "\n".join(lines)
    _validate_round_trip(spec, text)
    return text


def _validate_round_trip(spec: PassSpec, text: str) -> None:
    parsed = parse_toml_min(text)
    if parsed.get("model") != spec.model:
        raise ValueError("rendered codex config model does not match the spec")
    servers = parsed.get("mcp_servers", {})
    server = servers.get(spec.server_name, {})
    if server.get("command") != "selly-agent" or server.get("args") != ["mcp-proxy"]:
        raise ValueError("rendered codex config mcp server does not match the proxy invocation")
    expected = {spec.server_name}
    if spec.browser_server is not None:
        expected.add(spec.browser_server.name)
        browser = servers.get(spec.browser_server.name, {})
        if browser.get("command") != spec.browser_server.command:
            raise ValueError("rendered codex browser server command does not match the spec")
        if browser.get("args") != list(spec.browser_server.args):
            raise ValueError("rendered codex browser server args do not match the spec")
    if set(servers) != expected:
        raise ValueError("rendered codex config carries a server the spec did not ask for")


def parse_toml_min(text: str) -> dict:
    """A deliberately tiny TOML subset parser: dotted [section] headers and key = value lines,
    where a value is a quoted string, an integer, or a flat array of quoted strings. Enough to
    parse back exactly what render_config writes — not a general TOML implementation."""
    root: dict = {}
    section = root
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = root
            for part in line[1:-1].split("."):
                section = section.setdefault(part.strip(), {})
            continue
        if "=" not in line:
            raise ValueError(f"unparseable TOML line: {raw!r}")
        key, _, value = line.partition("=")
        section[key.strip()] = _parse_value(value.strip())
    return root


def _parse_value(token: str):
    if token.startswith("[") and token.endswith("]"):
        inner = token[1:-1].strip()
        if not inner:
            return []
        return [_parse_value(part.strip()) for part in inner.split(",")]
    if token.startswith('"') and token.endswith('"'):
        return token[1:-1]
    try:
        return int(token)
    except ValueError as exc:
        raise ValueError(f"unparseable TOML value: {token!r}") from exc
