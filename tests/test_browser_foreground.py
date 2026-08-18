"""The window raiser: finding the agent's Chrome by its CDP-port listener, activating exactly
that pid, and reading every failure — no lsof, no listener, a refused activation, a hung tool —
as a quiet False rather than an exception. The connect flow prints a hint on False; nothing here
may ever break a sign-in."""

from __future__ import annotations

import subprocess

import pytest

from sellee.browser import foreground


class RecordingRunner:
    """A subprocess.run stand-in: records each invocation, replays a scripted result per call.
    A step that is an exception instance is raised instead of returned."""

    def __init__(self, *script):
        self.calls = []
        self._script = list(script)

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if not self._script:
            pytest.fail(f"unexpected subprocess call: {argv}")
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def on_darwin(monkeypatch) -> None:
    monkeypatch.setattr(foreground.sys, "platform", "darwin")


def test_raise_window_finds_the_listener_on_the_cdp_port_and_activates_that_pid(
    monkeypatch,
) -> None:
    on_darwin(monkeypatch)
    runner = RecordingRunner(completed(stdout="12345\n"), completed())
    monkeypatch.setattr(foreground.subprocess, "run", runner)

    assert foreground.raise_window(9223) is True

    lsof_argv, _ = runner.calls[0]
    assert lsof_argv[0] == "lsof"
    assert "tcp:9223" in lsof_argv
    assert "-sTCP:LISTEN" in lsof_argv
    osascript_argv, _ = runner.calls[1]
    assert osascript_argv[0] == "osascript"
    assert ["-l", "JavaScript"] == osascript_argv[1:3]
    assert "12345" in osascript_argv[-1]
    # Both are argv lists, never a shell string — nothing lsof prints can be interpreted.
    assert all(isinstance(argv, list) for argv, _ in runner.calls)


def test_no_listener_means_false_and_osascript_is_never_run(monkeypatch) -> None:
    on_darwin(monkeypatch)
    runner = RecordingRunner(completed(returncode=1, stdout=""))
    monkeypatch.setattr(foreground.subprocess, "run", runner)

    assert foreground.raise_window(9222) is False
    assert len(runner.calls) == 1


def test_a_refused_activation_is_false_not_an_exception(monkeypatch) -> None:
    on_darwin(monkeypatch)
    runner = RecordingRunner(completed(stdout="12345\n"), completed(returncode=1))
    monkeypatch.setattr(foreground.subprocess, "run", runner)

    assert foreground.raise_window(9222) is False


def test_a_missing_lsof_is_a_quiet_false(monkeypatch) -> None:
    on_darwin(monkeypatch)
    runner = RecordingRunner(FileNotFoundError("lsof"))
    monkeypatch.setattr(foreground.subprocess, "run", runner)

    assert foreground.raise_window(9222) is False
    assert len(runner.calls) == 1


def test_a_hung_tool_is_cut_off_and_reads_as_false(monkeypatch) -> None:
    on_darwin(monkeypatch)
    runner = RecordingRunner(
        completed(stdout="12345\n"),
        subprocess.TimeoutExpired(cmd="osascript", timeout=5.0),
    )
    monkeypatch.setattr(foreground.subprocess, "run", runner)

    assert foreground.raise_window(9222) is False
    # Every subprocess carries a timeout: a raiser that can hang is worse than one that fails.
    assert all(kwargs.get("timeout") for _, kwargs in runner.calls)


def test_two_pids_on_the_port_use_the_first_and_still_activate_once(monkeypatch) -> None:
    on_darwin(monkeypatch)
    runner = RecordingRunner(completed(stdout="111\n222\n"), completed())
    monkeypatch.setattr(foreground.subprocess, "run", runner)

    assert foreground.raise_window(9222) is True
    assert len(runner.calls) == 2
    assert "111" in runner.calls[1][0][-1]


def test_garbage_from_lsof_never_reaches_osascript(monkeypatch) -> None:
    on_darwin(monkeypatch)
    runner = RecordingRunner(completed(stdout="not-a-pid\n"))
    monkeypatch.setattr(foreground.subprocess, "run", runner)

    assert foreground.raise_window(9222) is False
    assert len(runner.calls) == 1


def test_a_non_darwin_host_never_shells_out(monkeypatch) -> None:
    monkeypatch.setattr(foreground.sys, "platform", "linux")
    runner = RecordingRunner()  # any call fails the test
    monkeypatch.setattr(foreground.subprocess, "run", runner)

    assert foreground.raise_window(9222) is False
    assert runner.calls == []


@pytest.mark.parametrize(
    ("platform", "supported"),
    [("darwin", True), ("linux", False), ("win32", False)],
)
def test_is_supported_answers_for_the_os_before_anything_is_promised(
    monkeypatch, platform, supported
) -> None:
    """Callers ask this to decide what to *say*: a raise that cannot happen here is a different
    thing from one that was attempted and did not land."""
    monkeypatch.setattr(foreground.sys, "platform", platform)
    assert foreground.is_supported() is supported
