"""`harness config --attended`: what an attended session gets, and what it deliberately doesn't."""

from __future__ import annotations

import json
from argparse import Namespace

import pytest

from selly_agent import pass_cli, paths, secrets, skills


@pytest.fixture
def configured(xdg_tmp, tmp_path):
    """A written attended config in its own directory, with a token present."""
    paths.ensure_config_dir()
    secrets.write_secret(paths.mcp_token_path(), "ATTENDEDTOKEN")
    dest = tmp_path / "project"
    assert pass_cli.harness_config(Namespace(attended=True, dir=str(dest))) == 0
    return dest


def test_mcp_points_at_the_daemon_with_the_attended_token(configured) -> None:
    cfg = json.loads((configured / ".mcp.json").read_text())
    server = cfg["mcpServers"]["selly"]
    assert server["type"] == "http"
    assert server["url"].endswith("/mcp")
    assert server["headers"]["Authorization"] == "Bearer ATTENDEDTOKEN"


def test_the_five_commands_are_written(configured) -> None:
    written = sorted(p.stem for p in (configured / ".claude" / "commands").glob("*.md"))
    assert written == sorted(skills.available_commands())
    assert set(written) == {"catchup", "pause", "resume", "sell", "selly"}


def test_command_bodies_resolve_the_skill_paths_they_reference(configured) -> None:
    """A command that points at a skill file must point at one that exists — a broken path here
    is a session that silently loses its rulebook."""
    from pathlib import Path

    for body_path in (configured / ".claude" / "commands").glob("*.md"):
        body = body_path.read_text()
        assert "{SKILLS_DIR}" not in body  # every placeholder substituted
        for token in body.split("`"):
            if token.endswith(".md") and "/" in token:
                assert Path(token).is_file(), f"{body_path.name} references {token}"


def test_claude_md_asks_for_catchup_at_session_start(configured) -> None:
    text = (configured / "CLAUDE.md").read_text()
    assert "get_catchup" in text
    for name in ("sell", "catchup", "selly", "pause", "resume"):
        assert f"/{name}" in text


def test_claude_md_links_every_skill_by_a_resolvable_path(configured) -> None:
    from pathlib import Path

    text = (configured / "CLAUDE.md").read_text()
    referenced = [t for t in text.split("`") if t.endswith(".md") and "/" in t]
    assert len(referenced) == len(skills.available())
    for token in referenced:
        assert Path(token).is_file()


def test_no_settings_json_is_written(configured) -> None:
    """An attended session is the seller's own; its permission posture is theirs, not ours. The
    headless passes are where we assert a posture."""
    assert not (configured / ".claude" / "settings.json").exists()


def test_rerunning_refreshes_rather_than_duplicates(configured) -> None:
    stale = configured / ".claude" / "commands" / "sell.md"
    stale.write_text("stale content")
    assert pass_cli.harness_config(Namespace(attended=True, dir=str(configured))) == 0
    assert stale.read_text() != "stale content"


def test_without_a_token_nothing_is_written(xdg_tmp, tmp_path, capsys) -> None:
    dest = tmp_path / "project"
    assert pass_cli.harness_config(Namespace(attended=True, dir=str(dest))) == 1
    assert not dest.exists()
    assert "daemon" in capsys.readouterr().err


def test_paths_go_through_current_when_it_is_provisioned(xdg_tmp, tmp_path, monkeypatch) -> None:
    """The `current` symlink is the stable address for installed content: a command written today
    must still resolve after an update swaps the version underneath it."""
    checkout = tmp_path / "versions" / "v1"
    (checkout / "src" / "selly_agent" / "skills").mkdir(parents=True)
    paths.ensure_data_dirs()
    paths.current().symlink_to(checkout)

    assert pass_cli._skills_dir() == paths.current() / "src" / "selly_agent" / "skills"


def test_paths_fall_back_to_the_package_without_current(xdg_tmp) -> None:
    assert pass_cli._skills_dir() == skills.SKILLS_DIR
