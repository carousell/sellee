"""Harness emitters: golden argv/settings/mcp/toml, round-trip validators, and posture pins."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sellee.harness import claude, codex
from sellee.harness.model import PassSpec, StdioServer

GOLDEN = Path(__file__).parent / "golden"

_BROWSER = StdioServer(
    name="playwright",
    command="npx",
    args=("--yes", "@playwright/mcp", "--cdp-endpoint", "http://127.0.0.1:9222"),
    tools=("browser_navigate", "browser_click"),
)


def _spec(**overrides) -> PassSpec:
    base = dict(
        prompt="publish item item_123 using only your tools",
        model="sonnet",
        mcp_endpoint="http://127.0.0.1:7355/mcp",
        mcp_token="TESTTOKEN",
        allowed_tools=(
            "mcp__sellee__get_item",
            "mcp__sellee__carousell_ai_publish_listing",
            "mcp__sellee__send_message",
        ),
        max_turns=20,
    )
    base.update(overrides)
    return PassSpec(**base)


# --- spec validation --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"prompt": ""},
        {"model": ""},
        {"mcp_endpoint": "ftp://x"},
        {"mcp_token": ""},
        {"server_name": "bad name!"},
    ],
)
def test_passspec_rejects_malformed(overrides) -> None:
    with pytest.raises(ValueError):
        _spec(**overrides)


# --- claude goldens ---------------------------------------------------------------------------


def test_claude_pass_argv_matches_golden() -> None:
    argv = claude.pass_argv(_spec(), claude_bin="claude")
    assert argv == json.loads((GOLDEN / "claude_pass_argv.json").read_text())


def test_claude_workspace_matches_golden() -> None:
    files = claude.render_workspace(_spec())
    assert files[".mcp.json"] == (GOLDEN / "claude_mcp.json").read_text()
    assert files[".claude/settings.json"] == (GOLDEN / "claude_settings.json").read_text()


def test_allowed_tools_is_last_and_no_bash() -> None:
    argv = claude.pass_argv(_spec())
    idx = argv.index("--allowedTools")
    assert argv[idx + 1 :] == list(_spec().allowed_tools)  # nothing after the tool list
    assert "Bash" not in argv
    # settings deny the escape vectors explicitly
    deny = claude.settings_json(_spec())["permissions"]["deny"]
    assert "Bash" in deny


# --- web posture ------------------------------------------------------------------------------


def test_a_pass_without_web_tools_denies_them() -> None:
    perms = claude.settings_json(_spec())["permissions"]
    assert set(claude.WEB_TOOLS) <= set(perms["deny"])
    assert not set(claude.WEB_TOOLS) & set(perms["allow"])
    assert not set(claude.WEB_TOOLS) & set(claude.pass_argv(_spec()))


def test_a_web_enabled_pass_allows_them_and_stops_denying_them() -> None:
    spec = _spec(web_tools=True)
    files = claude.render_workspace(spec)
    assert files[".claude/settings.json"] == (GOLDEN / "claude_settings_web.json").read_text()
    assert claude.pass_argv(spec, claude_bin="claude") == json.loads(
        (GOLDEN / "claude_pass_argv_web.json").read_text()
    )


def test_web_tools_never_loosen_the_bash_posture() -> None:
    """The web flag moves two names between the lists and nothing else — Bash and the file tools
    stay denied whatever a pass type asks for."""
    for spec in (_spec(), _spec(web_tools=True)):
        deny = claude.settings_json(spec)["permissions"]["deny"]
        assert {"Bash", "Edit", "Write", "Read", "NotebookEdit"} <= set(deny)
        assert "Bash" not in claude.pass_argv(spec)


# --- read posture (granted media files) ---------------------------------------------------------


_PHOTOS = ("/media/store/95059966/photo.jpg", "/media/store/95059977/photo.jpg")


def test_a_pass_with_no_granted_media_denies_read_outright() -> None:
    perms = claude.settings_json(_spec())["permissions"]
    assert "Read" in perms["deny"]
    assert not any(rule.startswith("Read(") for rule in perms["allow"])


def test_granted_media_becomes_path_scoped_read_rules() -> None:
    spec = _spec(readable_paths=_PHOTOS)
    files = claude.render_workspace(spec)
    assert files[".claude/settings.json"] == (GOLDEN / "claude_settings_media.json").read_text()
    argv = claude.pass_argv(spec, claude_bin="claude")
    assert argv == json.loads((GOLDEN / "claude_pass_argv_media.json").read_text())
    # the // prefix anchors at the filesystem root — a single / would anchor at the settings file
    assert "Read(//media/store/95059966/photo.jpg)" in argv


def test_granted_media_lifts_the_bare_read_deny() -> None:
    """A deny beats any allow however specific, so bare Read must leave the deny list when paths
    are granted — anything outside the grants is still rejected (headless mode never prompts)."""
    perms = claude.settings_json(_spec(readable_paths=_PHOTOS))["permissions"]
    assert "Read" not in perms["deny"]
    rules = [rule for rule in perms["allow"] if rule.startswith("Read(")]
    assert rules == [f"Read(/{p})" for p in _PHOTOS]  # exactly the grants, nothing broader


def test_granted_media_never_loosens_the_rest_of_the_posture() -> None:
    perms = claude.settings_json(_spec(readable_paths=_PHOTOS))["permissions"]
    assert {"Bash", "Edit", "Write", "NotebookEdit"} <= set(perms["deny"])
    assert set(claude.WEB_TOOLS) <= set(perms["deny"])  # web stays off unless web_tools says so


def test_a_relative_readable_path_is_rejected_at_the_spec() -> None:
    with pytest.raises(ValueError, match="absolute"):
        _spec(readable_paths=("media/photo.jpg",))


def test_the_system_prompt_reaches_the_argv() -> None:
    argv = claude.pass_argv(_spec(append_system_prompt="RULEBOOK"))
    assert argv[argv.index("--append-system-prompt") + 1] == "RULEBOOK"


def test_stream_json_forces_verbose() -> None:
    argv = claude.pass_argv(_spec())
    assert "--verbose" in argv
    text_argv = claude.pass_argv(_spec(output_format="text"))
    assert "--verbose" not in text_argv


def test_token_is_in_the_header_not_bare() -> None:
    cfg = claude.mcp_config(_spec())
    assert cfg["mcpServers"]["sellee"]["headers"]["Authorization"] == "Bearer TESTTOKEN"


# --- the second (browser) MCP server ------------------------------------------------------------


def test_a_browser_pass_renders_both_servers() -> None:
    from sellee import passes

    spec = _spec(
        browser_server=StdioServer(
            name="playwright",
            command="npx",
            args=("--yes", "@playwright/mcp", "--cdp-endpoint", "http://127.0.0.1:9222"),
            tools=passes.PUBLISH_BROWSER_TOOLS,
        )
    )
    files = claude.render_workspace(spec)
    assert files[".mcp.json"] == (GOLDEN / "claude_mcp_browser.json").read_text()
    assert files[".claude/settings.json"] == (GOLDEN / "claude_settings_browser.json").read_text()
    assert claude.pass_argv(spec, claude_bin="claude") == json.loads(
        (GOLDEN / "claude_pass_argv_browser.json").read_text()
    )


def test_a_pass_with_no_browser_renders_only_our_server() -> None:
    servers = claude.mcp_config(_spec())["mcpServers"]
    assert set(servers) == {"sellee"}


def test_the_browser_server_is_stdio_not_a_port() -> None:
    """A localhost browser-control port would be an unauthenticated way to drive the seller's
    Chrome; a stdio subprocess is reachable only by its parent."""
    browser = claude.mcp_config(_spec(browser_server=_BROWSER))["mcpServers"]["playwright"]
    assert browser["type"] == "stdio" and browser["command"] == "npx"
    assert "url" not in browser and "headers" not in browser


def test_the_browser_diet_becomes_allow_list_rules() -> None:
    allowed = claude.allowed_tools(_spec(browser_server=_BROWSER))
    assert "mcp__playwright__browser_navigate" in allowed
    assert "mcp__playwright__browser_click" in allowed
    # a tool the diet leaves out has no rule, so the pass cannot call it
    assert not any(rule.endswith("browser_run_code_unsafe") for rule in allowed)


def test_the_diet_excludes_the_tools_that_would_undo_the_posture() -> None:
    """browser_close would shut the seller's warm Chrome; run_code_unsafe is arbitrary Playwright
    code — the browser's version of the shell this whole surface exists to replace."""
    from sellee import passes

    assert "browser_close" not in passes.PUBLISH_BROWSER_TOOLS
    assert "browser_run_code_unsafe" not in passes.PUBLISH_BROWSER_TOOLS
    assert "browser_take_screenshot" not in passes.PUBLISH_BROWSER_TOOLS


def test_a_browser_server_with_no_tools_is_refused_at_the_spec() -> None:
    """Reaching a server with nothing allowed is authority with no use for it."""
    with pytest.raises(ValueError, match="at least one tool"):
        _spec(browser_server=StdioServer(name="playwright", command="npx", tools=()))


def test_a_browser_server_may_not_shadow_our_own() -> None:
    with pytest.raises(ValueError, match="must not share"):
        _spec(browser_server=StdioServer(name="sellee", command="npx", tools=("browser_navigate",)))


def test_the_validator_catches_an_unrequested_server() -> None:
    """With --strict-mcp-config the rendered set IS the reachable surface, so an extra entry is an
    authority grant no pass type asked for."""
    spec = _spec()
    files = claude.render_workspace(spec)
    tampered = json.loads(files[".mcp.json"])
    tampered["mcpServers"]["sneaky"] = {"type": "stdio", "command": "sh"}
    with pytest.raises(ValueError, match="did not ask for"):
        claude._validate_servers(spec, tampered)


def test_a_browser_pass_keeps_the_no_bash_posture() -> None:
    spec = _spec(browser_server=_BROWSER)
    deny = claude.settings_json(spec)["permissions"]["deny"]
    assert {"Bash", "Edit", "Write", "NotebookEdit"} <= set(deny)
    assert "Bash" not in claude.pass_argv(spec)


def test_allowed_tools_stays_last_with_a_browser_server() -> None:
    spec = _spec(browser_server=_BROWSER, readable_paths=("/media/store/1/a.jpg",))
    argv = claude.pass_argv(spec)
    idx = argv.index("--allowedTools")
    assert list(argv[idx + 1 :]) == list(claude.allowed_tools(spec))


# --- codex golden + round trip ----------------------------------------------------------------


def test_codex_config_matches_golden() -> None:
    assert codex.render_config(_spec()) == (GOLDEN / "codex_config.toml").read_text()


def test_codex_round_trips_and_points_at_proxy() -> None:
    parsed = codex.parse_toml_min(codex.render_config(_spec(model="opus")))
    assert parsed["model"] == "opus"
    assert parsed["mcp_servers"]["sellee"] == {"command": "sellee", "args": ["mcp-proxy"]}


def test_codex_carries_the_browser_server_too() -> None:
    """Keeping the second emitter honest is what forces PassSpec to stay genuinely common; Codex has
    no spawn path yet, but the shape it renders is the same one."""
    from sellee import passes

    spec = _spec(
        browser_server=StdioServer(
            name="playwright",
            command="npx",
            args=("--yes", "@playwright/mcp", "--cdp-endpoint", "http://127.0.0.1:9222"),
            tools=passes.PUBLISH_BROWSER_TOOLS,
        )
    )
    assert codex.render_config(spec) == (GOLDEN / "codex_config_browser.toml").read_text()
    parsed = codex.parse_toml_min(codex.render_config(spec))
    assert parsed["mcp_servers"]["playwright"]["command"] == "npx"


def test_toml_min_parser_handles_the_subset() -> None:
    text = 'a = "x"\nn = 3\n[s.t]\narr = ["p", "q"]\n'
    parsed = codex.parse_toml_min(text)
    assert parsed == {"a": "x", "n": 3, "s": {"t": {"arr": ["p", "q"]}}}
