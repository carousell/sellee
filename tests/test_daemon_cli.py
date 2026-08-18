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
