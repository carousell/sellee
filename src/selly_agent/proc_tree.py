"""Stop a spawned pass and everything under it, and reap the ones nobody is tracking.

A pass is a `claude` process that spawns children of its own, and those children do the work that
touches the seller's live account. Signalling only the leader leaves them running after the daemon
has moved on, so a stop is always tree-wide and a forced one waits until the tree is gone rather
than assuming it.

Descendants are listed before anything is signalled, because a child reparented after its parent
dies is no longer reachable from it. On POSIX the process group is signalled as well, which covers
a descendant that reparents in the moment between the listing and the signal; Windows has no
equivalent, so there the listing is all there is.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import time

import psutil

from selly_agent import host

log = logging.getLogger(__name__)

_GRACE_SEC = 10  # a terminated tree gets this long to exit before it is killed outright
_KILL_WAIT_SEC = 5  # after the kill, how long to wait for confirmation

# Resolved with getattr because the attribute does not exist on Windows — and it must not be
# spelled at a call site, where it would be evaluated (and raise) before _signal_group could
# decline. The group is always None there, so the constant is only used where it is defined.
_SIGKILL = getattr(signal, "SIGKILL", None)


def _running(proc) -> bool:
    """Whether a process is still doing something.

    A zombie is not: it has exited and is only waiting for its parent to collect the status. When
    the parent was killed alongside it there may be nobody left to do that, so counting one as
    alive would mean reporting a tree we did kill as surviving, and re-killing it every tick.
    """
    try:
        return proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        return True  # it exists and will not say more; assume the worse


def _descendants(pid: int) -> list:
    try:
        return psutil.Process(pid).children(recursive=True)
    except psutil.Error:
        return []


def _process_group(pid: int) -> int | None:
    """The pass's own process group, or None where there is no such thing.

    Read before anything is signalled: once the leader is reaped its PID no longer answers.
    """
    if host.windows():
        return None
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def _signal_group(group: int | None, sig: int | None) -> None:
    if group is None or sig is None:
        return
    with contextlib.suppress(OSError):
        os.killpg(group, sig)


def _stop(procs: list) -> None:
    """Ask each process to exit. On Windows this is already a hard termination — the platform has
    no graceful equivalent — which is why the pass is asked over its own channel first."""
    for proc in procs:
        with contextlib.suppress(psutil.Error):
            proc.terminate()


def _kill(procs: list) -> None:
    for proc in procs:
        with contextlib.suppress(psutil.Error):
            proc.kill()


def confirm_dead(proc, grace: float = _GRACE_SEC) -> bool:
    """Stop a pass we spawned, and do not return until it and its descendants are gone.

    Answers whether they actually are. A caller that forgets the ledger record on a False
    would be throwing away the only handle anything has on the survivors.

    The leader is stopped and reaped through its own Popen rather than through psutil, which would
    reap it out from under subprocess and leave the exit status unreadable.
    """
    if proc.pid is None:
        return True
    children = _descendants(proc.pid)
    group = _process_group(proc.pid)

    _stop(children)
    with contextlib.suppress(OSError):
        proc.terminate()
    _signal_group(group, signal.SIGTERM)

    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=grace)
    _, alive = psutil.wait_procs(children, timeout=grace)
    if not alive and proc.poll() is not None:
        return True

    _kill(alive)
    with contextlib.suppress(OSError):
        proc.kill()
    _signal_group(group, _SIGKILL)

    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=_KILL_WAIT_SEC)
    _, survivors = psutil.wait_procs(alive, timeout=_KILL_WAIT_SEC)
    left = [proc_ for proc_ in survivors if _running(proc_)]
    if left or proc.poll() is None:
        log.error(
            "pass pid=%s left %s process(es) alive after being killed",
            proc.pid,
            len(left) + (0 if proc.poll() is not None else 1),
        )
        return False
    return True


def kill_tree(pid: int, grace: float = _GRACE_SEC) -> bool:
    """Stop a process we did not spawn, and its descendants. True when nothing is left.

    Used on a pass a previous daemon left behind: there is no Popen for it, so its exit status is
    nobody's to read and psutil owns the whole operation.
    """
    try:
        leader = psutil.Process(pid)
    except psutil.Error:
        return True
    procs = [*_descendants(pid), leader]
    group = _process_group(pid)

    _stop(procs)
    _signal_group(group, signal.SIGTERM)
    _, alive = psutil.wait_procs(procs, timeout=grace)
    if not alive:
        return True

    _kill(alive)
    _signal_group(group, _SIGKILL)
    _, survivors = psutil.wait_procs(alive, timeout=_KILL_WAIT_SEC)
    left = [proc for proc in survivors if _running(proc)]
    for proc in left:
        log.error("process %s survived being killed", proc.pid)
    return not left


# --- the stray reaper -------------------------------------------------------------------------


def creation_time(pid: int) -> float | None:
    """When the OS says the process holding `pid` started, or None if it is already gone."""
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


def is_recorded_process(pid: int, created_ts: float, *, tolerance: float = 1.0) -> bool:
    """Whether the process now holding `pid` is the one recorded at `created_ts`.

    PIDs are reused, so the creation time is the identity: without this check a reaper could kill
    an unrelated process — a browser, a build — that happens to have inherited the number. The
    tolerance covers the platforms that report creation time at a coarser resolution than they
    record it.
    """
    try:
        proc = psutil.Process(pid)
        return _running(proc) and abs(proc.create_time() - created_ts) <= tolerance
    except psutil.Error:
        return False


def find_stray_passes(records, now: float | None = None) -> list:
    """Recorded pass processes that are still alive well past the deadline they were given.

    A pass this old cannot be legitimate: a tracked one would have been killed at its deadline by
    the daemon that started it, so what is left is what a daemon that died could not clean up.
    """
    moment = time.time() if now is None else now
    strays = []
    for record in records:
        if moment <= record["reap_after_ts"]:
            continue
        if not is_recorded_process(record["pid"], record["created_ts"]):
            continue
        strays.append(record)
    return strays


def finished_records(records, now: float | None = None) -> list:
    """Records past their deadline whose process is no longer the one recorded.

    Either it exited on its own or its pid has been handed to somebody else; both mean there is
    nothing here to kill and nothing more to watch. They are separated from the strays because
    only a kill is worth an event — but they still have to be forgotten, and until now nothing
    deleted them, so the table grew by one row for every pass whose daemon died mid-flight.
    """
    moment = time.time() if now is None else now
    return [
        record
        for record in records
        if moment > record["reap_after_ts"]
        and not is_recorded_process(record["pid"], record["created_ts"])
    ]


def reap_strays(records, now: float | None = None) -> list:
    """Kill every stray pass found, and answer which ones are confirmed gone.

    A stray that survives the kill stays out of the answer on purpose: its record is the only
    thing that lets the next tick find it again, so it must not be forgotten on a failed kill.
    """
    reaped = []
    for stray in find_stray_passes(records, now=now):
        log.warning(
            "reaping stray pass %s pid=%s (untracked and past its deadline)",
            stray["pass_id"],
            stray["pid"],
        )
        if kill_tree(stray["pid"]):
            reaped.append(stray)
    return reaped
