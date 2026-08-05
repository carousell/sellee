"""proc_tree: killing a real tree that refuses to go, and the reaper's identity check.

The kills run against real processes rather than mocks — the whole point of this module is what the
OS does, and a fake that terminates on request would prove nothing about the escalation.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from selly_agent import proc_tree, spawn

_IGNORE_SIGTERM = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('ready', flush=True)\n"
    "time.sleep(60)\n"
)

# Prints once its child is up, so a test knows the tree exists before killing it.
_SPAWN_A_CHILD = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(60)\n"
)


def _spawn(source: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", source],
        stdout=subprocess.PIPE,
        text=True,
        **spawn.detached_flags(),
    )


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX process groups")
def test_confirm_dead_escalates_past_a_process_ignoring_the_request() -> None:
    proc = _spawn(_IGNORE_SIGTERM)
    assert proc.stdout.readline().strip() == "ready"  # the handler is installed
    group = os.getpgid(proc.pid)

    proc_tree.confirm_dead(proc, grace=0.5)

    assert proc.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(group, 0)


def _stopped(pid: int, created_ts: float, timeout: float = 5.0) -> bool:
    """Whether the process is no longer running anything — gone, or a zombie waiting to be reaped
    by a parent that was killed with it (which is all a container without a reaping init leaves)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not proc_tree.is_recorded_process(pid, created_ts):
            return True
        time.sleep(0.05)
    return not proc_tree.is_recorded_process(pid, created_ts)


def test_confirm_dead_takes_the_children_with_it() -> None:
    """The child is the one doing the work — a stop that leaves it running leaves the seller's
    account being acted on by a pass the daemon has already written off."""
    proc = _spawn(_SPAWN_A_CHILD)
    child_pid = int(proc.stdout.readline().strip())
    created = proc_tree.creation_time(child_pid)
    assert created is not None

    proc_tree.confirm_dead(proc, grace=5)

    assert proc.poll() is not None
    assert _stopped(child_pid, created)


def test_confirm_dead_returns_promptly_when_asked_nicely() -> None:
    proc = _spawn("import time; print('ready', flush=True); time.sleep(60)")
    assert proc.stdout.readline().strip() == "ready"

    started = time.monotonic()
    proc_tree.confirm_dead(proc, grace=5)

    assert proc.poll() is not None
    assert time.monotonic() - started < 4  # it went on the first ask; no escalation wait


def test_confirm_dead_on_an_already_finished_pass_is_quiet() -> None:
    proc = _spawn("pass")
    proc.wait(timeout=10)
    proc_tree.confirm_dead(proc, grace=0.1)


def test_kill_tree_of_something_already_gone_is_true() -> None:
    proc = _spawn("pass")
    pid = proc.pid
    proc.wait(timeout=10)
    assert proc_tree.kill_tree(pid) is True


# --- the reaper's identity check ---------------------------------------------------------------


def test_a_recorded_process_is_recognised_by_its_creation_time() -> None:
    own = os.getpid()
    created = proc_tree.creation_time(own)
    assert created is not None
    assert proc_tree.is_recorded_process(own, created)
    # A PID is not an identity: the same number with a different birthday is a different process,
    # which is what stops a reaper killing whatever inherited the number.
    assert not proc_tree.is_recorded_process(own, created - 3600)


def test_creation_time_of_a_process_that_is_not_there_is_unknown() -> None:
    assert proc_tree.creation_time(2**31 - 1) is None


def _record(pass_id: str, pid: int, created_ts: float, reap_after_ts: float) -> dict:
    return {
        "pass_id": pass_id,
        "pid": pid,
        "created_ts": created_ts,
        "reap_after_ts": reap_after_ts,
    }


def test_only_records_past_their_deadline_are_strays() -> None:
    own = os.getpid()
    created = proc_tree.creation_time(own)
    records = [
        _record("pass_old", own, created, reap_after_ts=100.0),
        _record("pass_running", own, created, reap_after_ts=900.0),
    ]

    strays = proc_tree.find_stray_passes(records, now=500.0)

    assert [s["pass_id"] for s in strays] == ["pass_old"]


def test_a_reused_pid_is_never_a_stray() -> None:
    """The daemon this record outlived may have died days ago, by which time the number belongs to
    something else entirely — a browser, a build, another agent."""
    own = os.getpid()
    records = [_record("pass_old", own, proc_tree.creation_time(own) - 3600, reap_after_ts=100.0)]

    assert proc_tree.find_stray_passes(records, now=500.0) == []


def test_a_record_whose_process_is_gone_is_not_a_stray() -> None:
    records = [_record("pass_old", 2**31 - 1, 1.0, reap_after_ts=100.0)]

    assert proc_tree.find_stray_passes(records, now=500.0) == []


def test_reap_strays_kills_and_reports_each_one(monkeypatch) -> None:
    killed = []
    monkeypatch.setattr(proc_tree, "kill_tree", lambda pid: killed.append(pid) or True)
    own = os.getpid()
    records = [_record("pass_old", own, proc_tree.creation_time(own), reap_after_ts=100.0)]

    reaped = proc_tree.reap_strays(records, now=500.0)

    assert [r["pass_id"] for r in reaped] == ["pass_old"]
    assert killed == [own]
