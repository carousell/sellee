"""`selly-agent chat`: the gates it refuses at, and what it hands to Claude Code."""

from __future__ import annotations

import json

import pytest

from selly_agent import config, control, pass_cli, paths, secrets


@pytest.fixture
def launched(monkeypatch):
    """Records the exec instead of becoming a Claude Code session."""
    calls = []
    monkeypatch.setattr(pass_cli, "_exec", lambda binary, argv: calls.append((binary, argv)))
    return calls


@pytest.fixture
def ready(xdg_tmp, monkeypatch):
    """A token present, a daemon that answers, and a resolvable `claude`."""
    paths.ensure_config_dir()
    secrets.write_secret(paths.mcp_token_path(), "ATTENDEDTOKEN")
    monkeypatch.setattr(control, "get", lambda *a, **k: (200, {"bound": False}))
    monkeypatch.setattr("selly_agent.passes.resolve_claude_bin", lambda cfg: "/usr/bin/claude")


def test_it_launches_claude_in_the_attended_workspace(ready, launched, monkeypatch) -> None:
    chdir_to = []
    monkeypatch.setattr(pass_cli.os, "chdir", chdir_to.append)

    assert pass_cli.chat() == 0
    assert chdir_to == [pass_cli.attended_dir()]
    assert launched == [("/usr/bin/claude", ["/usr/bin/claude"])]


def test_the_workspace_is_regenerated_at_launch(ready, launched, monkeypatch) -> None:
    """An update or a rotated token would otherwise leave the session pointed at nothing."""
    monkeypatch.setattr(pass_cli.os, "chdir", lambda _: None)
    dest = pass_cli.attended_dir()
    dest.mkdir(parents=True)
    (dest / ".mcp.json").write_text('{"mcpServers": {"selly": {"url": "http://stale"}}}')

    assert pass_cli.chat() == 0
    server = json.loads((dest / ".mcp.json").read_text())["mcpServers"]["selly"]
    assert server["headers"]["Authorization"] == "Bearer ATTENDEDTOKEN"
    assert str(config.load().http_port) in server["url"]


def test_a_stopped_daemon_is_refused_before_anything_launches(
    xdg_tmp, launched, monkeypatch, capsys
) -> None:
    """Every tool the session has is served by the daemon: launched without one it comes up inert
    with no visible reason why."""
    paths.ensure_config_dir()
    secrets.write_secret(paths.mcp_token_path(), "ATTENDEDTOKEN")

    def unreachable(*a, **k):
        raise control.DaemonUnreachable("connection refused")

    monkeypatch.setattr(control, "get", unreachable)

    assert pass_cli.chat() == 1
    assert launched == []
    assert "daemon start" in capsys.readouterr().err
    assert not pass_cli.attended_dir().exists()


def test_a_rejected_token_reports_the_daemons_own_reason(
    xdg_tmp, launched, monkeypatch, capsys
) -> None:
    paths.ensure_config_dir()
    secrets.write_secret(paths.mcp_token_path(), "STALE")
    monkeypatch.setattr(control, "get", lambda *a, **k: (401, {"error": "unauthorized"}))

    assert pass_cli.chat() == 1
    assert launched == []
    assert "unauthorized" in capsys.readouterr().err


def test_without_a_token_it_says_the_daemon_has_never_run(xdg_tmp, launched, capsys) -> None:
    assert pass_cli.chat() == 1
    assert launched == []
    assert "daemon" in capsys.readouterr().err


def test_a_missing_claude_binary_is_reported_not_execed(
    ready, launched, monkeypatch, capsys
) -> None:
    monkeypatch.setattr("selly_agent.passes.resolve_claude_bin", lambda cfg: None)

    assert pass_cli.chat() == 1
    assert launched == []
    assert "claude binary not found" in capsys.readouterr().err
