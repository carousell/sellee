"""Harness emitters: golden argv/settings/mcp/toml, round-trip validators, and posture pins."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from selly_agent.harness import claude, codex
from selly_agent.harness.model import PassSpec

GOLDEN = Path(__file__).parent / "golden"


def _spec(**overrides) -> PassSpec:
    base = dict(
        prompt="publish item item_123 using only your tools",
        model="sonnet",
        mcp_endpoint="http://127.0.0.1:7355/mcp",
        mcp_token="TESTTOKEN",
        allowed_tools=(
            "mcp__selly__get_item",
            "mcp__selly__carousell_ai_publish_listing",
            "mcp__selly__send_message",
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
    assert cfg["mcpServers"]["selly"]["headers"]["Authorization"] == "Bearer TESTTOKEN"


# --- codex golden + round trip ----------------------------------------------------------------


def test_codex_config_matches_golden() -> None:
    assert codex.render_config(_spec()) == (GOLDEN / "codex_config.toml").read_text()


def test_codex_round_trips_and_points_at_proxy() -> None:
    parsed = codex.parse_toml_min(codex.render_config(_spec(model="opus")))
    assert parsed["model"] == "opus"
    assert parsed["mcp_servers"]["selly"] == {"command": "selly-agent", "args": ["mcp-proxy"]}


def test_toml_min_parser_handles_the_subset() -> None:
    text = 'a = "x"\nn = 3\n[s.t]\narr = ["p", "q"]\n'
    parsed = codex.parse_toml_min(text)
    assert parsed == {"a": "x", "n": 3, "s": {"t": {"arr": ["p", "q"]}}}
