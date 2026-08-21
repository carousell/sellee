"""`sellee daemon <verb>` — and which of those verbs a container has no answer for.

Registering, starting and stopping are launchd's vocabulary. In a container the process is
Docker's, so a verb that appeared to work would either do nothing or leave the seller believing
they had stopped an agent that is still running. Status is the exception: it is a question, not a
command, and it has a truthful answer either way.
"""

from __future__ import annotations

import argparse

import pytest

from sellee import daemon_cli, supervisor


def _args(command: str) -> argparse.Namespace:
    return argparse.Namespace(daemon_command=command, label=None, mode="login-start", once=False)


@pytest.fixture
def no_supervisor(monkeypatch):
    """Any call into the supervisor is a failure of the refusal — nothing should reach it."""

    def _explode(*args, **kwargs):
        raise AssertionError("the supervisor was called in container mode")

    for verb in ("install", "uninstall", "start", "stop"):
        monkeypatch.setattr(supervisor, verb, _explode)


@pytest.mark.parametrize("command", ["install", "start", "stop", "uninstall"])
def test_a_supervisor_verb_is_refused_and_says_whose_job_it_is(
    command, container, no_supervisor, capsys
) -> None:
    assert daemon_cli.dispatch(_args(command)) == 2
    err = capsys.readouterr().err
    assert "container runtime" in err
    # Never a command: we do not know the engine, and we did not choose the container's name.
    assert "docker" not in err.lower()


def test_status_still_answers_in_a_container(container, xdg_tmp, capsys) -> None:
    assert daemon_cli.dispatch(_args("status")) == 0
    out = capsys.readouterr().out
    assert "mode:      container" in out
    assert "state:     stopped" in out  # nothing is running under a fresh tmp root


def test_the_host_verbs_are_untouched(monkeypatch, capsys) -> None:
    called = []
    monkeypatch.setattr(supervisor, "start", lambda **kwargs: called.append("start") or 0)
    assert daemon_cli.dispatch(_args("start")) == 0
    assert called == ["start"]


# --- rotate-token --------------------------------------------------------------------------------


def test_rotate_token_goes_through_a_running_daemon(xdg_tmp, monkeypatch, capsys) -> None:
    """The daemon holds an in-memory copy, so rotation must go through it when it is up."""
    from sellee import control, paths, secrets

    paths.ensure_config_dir()
    secrets.write_secret(paths.mcp_token_path(), "OLDTOKEN")
    posts = []

    def fake_post(port, token, route, body, **kw):
        posts.append((route, token))
        return 200, {"status": "rotated"}

    monkeypatch.setattr(control, "post", fake_post)

    assert daemon_cli.dispatch(_args("rotate-token")) == 0
    assert posts == [("/control/rotate-token", "OLDTOKEN")]
    out = capsys.readouterr().out
    assert "rotated" in out
    # the CLI never rewrites the file itself on this path — that is the daemon's job
    assert secrets.read_mcp_token() == "OLDTOKEN"


def test_rotate_token_falls_back_to_the_file_when_no_daemon_answers(
    xdg_tmp, monkeypatch, capsys
) -> None:
    from sellee import control, paths, secrets

    paths.ensure_config_dir()
    secrets.write_secret(paths.mcp_token_path(), "OLDTOKEN")

    def unreachable(*a, **k):
        raise control.DaemonUnreachable("connection refused")

    monkeypatch.setattr(control, "post", unreachable)

    assert daemon_cli.dispatch(_args("rotate-token")) == 0
    assert secrets.read_mcp_token() != "OLDTOKEN"
    assert "next start" in capsys.readouterr().out


def test_rotate_token_without_a_token_explains_and_fails(xdg_tmp, capsys) -> None:
    assert daemon_cli.dispatch(_args("rotate-token")) == 1
    assert "no MCP token found" in capsys.readouterr().err
