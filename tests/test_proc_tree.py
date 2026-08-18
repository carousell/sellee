"""proc_tree: SIGKILL escalation against a SIGTERM-ignoring group, and the stray-reaper matrix."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from sellee import proc_tree

_IGNORE_SIGTERM = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('ready', flush=True)\n"
    "time.sleep(60)\n"
)


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX process groups")
def test_confirm_dead_escalates_to_sigkill_and_group_gone() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", _IGNORE_SIGTERM],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert proc.stdout.readline().strip() == "ready"  # wait until the handler is installed
    pgid = os.getpgid(proc.pid)

    # grace short so the test doesn't wait the full default; SIGTERM is ignored -> SIGKILL wins.
    proc_tree.confirm_dead(proc, grace=0.5)

    assert proc.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)  # whole group is gone


def test_find_stray_passes_matches_marker_pgid_and_age() -> None:
    marker = proc_tree.PASS_PROMPT_MARKER
    snapshot = "\n".join(
        [
            f"111 111 20:00 claude -p {marker} and more",  # stray: old, untracked, marked
            f"222 222 00:05 claude -p {marker}",  # too young
            "333 333 20:00 claude -p some other prompt",  # marked absent
            f"444 444 20:00 python worker {marker}",  # not claude -p
            f"555 555 20:00 claude -p {marker}",  # tracked -> excluded
        ]
    )
    strays = proc_tree.find_stray_passes(known_pgids={555}, min_age_sec=600, snapshot=snapshot)
    assert [s["pid"] for s in strays] == [111]
    assert strays[0]["age_sec"] == 20 * 60


def test_find_stray_passes_empty_on_blank_snapshot() -> None:
    assert proc_tree.find_stray_passes(set(), 600, snapshot="") == []


def test_reap_strays_returns_reaped(monkeypatch) -> None:
    marker = proc_tree.PASS_PROMPT_MARKER
    snapshot = f"999 999 30:00 claude -p {marker}\n"
    killed = []
    monkeypatch.setattr(proc_tree, "_killpg", lambda pgid, sig: killed.append((pgid, sig)))
    reaped = proc_tree.reap_strays(set(), 600, snapshot=snapshot)
    assert [r["pid"] for r in reaped] == [999]
    assert killed == [(999, __import__("signal").SIGKILL)]


def test_etime_parsing() -> None:
    assert proc_tree._etime_sec("00:05") == 5
    assert proc_tree._etime_sec("01:02:03") == 3723
    assert proc_tree._etime_sec("2-00:00:00") == 172800
    assert proc_tree._etime_sec("garbage") is None


def _sleep_forever() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX process groups")
def test_confirm_dead_sigterm_path_is_quick() -> None:
    proc = _sleep_forever()
    assert proc.stdout.readline().strip() == "ready"
    started = time.monotonic()
    proc_tree.confirm_dead(proc, grace=5)
    assert proc.poll() is not None
    assert time.monotonic() - started < 4  # SIGTERM alone dispatched it, no SIGKILL wait
