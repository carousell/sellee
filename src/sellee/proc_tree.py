"""Deterministically kill a spawned pass and its whole process group, and reap strays.

A pass is spawned with start_new_session=True, so it leads its own group (pgid == pid). The
`claude` grandchild that does the real work would be orphaned if we signalled only the leader's
pid — still acting on the live account after the daemon moved on. So we always signal the GROUP
and, on a forced stop, wait until the whole group is gone before returning.

The stray reaper is the backstop: any headless pass group the current runtime does not track,
older than its deadline plus slack, cannot be legitimate (a tracked pass would already have been
killed at its deadline). An unsignalable group is treated as alive (fail conservative).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time

log = logging.getLogger(__name__)

_GRACE_SEC = 10  # a SIGTERM'd group gets this long to exit before SIGKILL
_KILL_WAIT_SEC = 5  # after SIGKILL, how long we wait to confirm the group is gone
_POLL_SEC = 0.05


def _killpg(pgid: int | None, sig: int) -> None:
    if pgid is None:
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # exists but unsignalable — treat as alive (conservative)


def _wait_group_gone(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return True
        time.sleep(_POLL_SEC)
    return not _group_alive(pgid)


def confirm_dead(proc, grace: float = _GRACE_SEC) -> None:
    """SIGTERM the group, then wait until the WHOLE tree is gone — escalating to SIGKILL after
    `grace`. The pgid is captured before the leader is reaped (its pid vanishes once reaped)."""
    try:
        pgid = os.getpgid(proc.pid)
    except (OSError, TypeError):
        pgid = None
    _killpg(pgid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)  # reap the leader so its zombie can't read as 'alive'
    except subprocess.TimeoutExpired:
        pass
    if pgid is None or _wait_group_gone(pgid, grace):
        return
    _killpg(pgid, signal.SIGKILL)
    try:
        proc.wait(timeout=_KILL_WAIT_SEC)
    except subprocess.TimeoutExpired:
        pass
    if not _wait_group_gone(pgid, _KILL_WAIT_SEC):
        log.error("process group %s survived SIGKILL", pgid)


# --- stray-pass reaper ------------------------------------------------------------------------

# Embedded in every pass prompt so the reaper can positively identify our headless passes.
PASS_PROMPT_MARKER = "sellee headless pass"


def _ps_snapshot() -> str:
    """One ps scan of every process (pid, pgid, etime, command). Fail-open to ''."""
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,pgid=,etime=,command="],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout if out.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        return ""


def _etime_sec(s: str) -> int | None:
    """Parse ps etime ([[dd-]hh:]mm:ss) to seconds, or None on garbage."""
    try:
        days = 0
        body = s.strip()
        if "-" in body:
            d, body = body.split("-", 1)
            days = int(d)
        parts = [int(p) for p in body.split(":")]
    except ValueError:
        return None
    if len(parts) == 3:
        h, m, sec = parts
    elif len(parts) == 2:
        h, m, sec = 0, parts[0], parts[1]
    else:
        return None
    return ((days * 24 + h) * 60 + m) * 60 + sec


def find_stray_passes(known_pgids, min_age_sec: float, snapshot: str | None = None) -> list:
    """Headless pass processes (`claude -p` carrying our marker) whose pgid is neither tracked nor
    our own group, older than `min_age_sec`. `snapshot` is injectable for tests. Returns
    [{pid, pgid, age_sec}]."""
    strays = []
    known = set(known_pgids)
    try:
        own = os.getpgid(0)
    except OSError:
        own = None
    text = snapshot if snapshot is not None else _ps_snapshot()
    for line in text.splitlines():
        bits = line.strip().split(None, 3)
        if len(bits) < 4:
            continue
        pid_s, pgid_s, etime_s, cmd = bits
        if "claude -p" not in cmd or PASS_PROMPT_MARKER not in cmd:
            continue
        try:
            pid, pgid = int(pid_s), int(pgid_s)
        except ValueError:
            continue
        age = _etime_sec(etime_s)
        if age is None or age < min_age_sec:
            continue
        if pgid in known or pgid == own:
            continue
        strays.append({"pid": pid, "pgid": pgid, "age_sec": age})
    return strays


def reap_strays(known_pgids, min_age_sec: float, snapshot: str | None = None) -> list:
    """SIGKILL every stray pass group found (they already outlived their deadline). Returns the
    reaped list."""
    reaped = []
    for stray in find_stray_passes(known_pgids, min_age_sec, snapshot=snapshot):
        log.warning(
            "reaping stray pass pid=%s pgid=%s age=%ss (untracked and past deadline)",
            stray["pid"],
            stray["pgid"],
            stray["age_sec"],
        )
        _killpg(stray["pgid"], signal.SIGKILL)
        reaped.append(stray)
    return reaped
