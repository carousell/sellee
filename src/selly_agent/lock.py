"""Single-instance lock — PID-aware, so launchd never storms a respawn.

launchd keeps the daemon alive, so a second instance can collide with a live one. The rules
that keep that from becoming a respawn storm: a duplicate colliding with a live holder reports
the truthful holder and the caller exits 0 (a clean exit KeepAlive won't respawn); a lock left
by a dead PID is reclaimed, not refused (a restart on fresh code never wedges behind a corpse);
a clean shutdown clears the body only when we are the recorded holder. The fd stays referenced
for the process lifetime — the OS releases the flock on exit/crash.

Simplified from the legacy primitive: there is no watchdog here, so no heartbeat
cross-stamping — just the flock, the PID body, and the reclaim rule.
"""

from __future__ import annotations

import fcntl
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import psutil

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LockResult:
    acquired: bool
    holder_pid: int | None
    holder_alive: bool
    reclaimed: bool
    fd: int | None


def is_pid_alive(pid: int | None) -> bool:
    """True if a process with `pid` exists — including one owned by another user, and including
    a zombie the daemon has not been reaped yet. Garbage (None, 0, negative) → dead."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    return psutil.pid_exists(pid)


def read_holder_pid(lock_path: Path) -> int | None:
    """The PID recorded in the lock body, or None on a missing/empty/garbage body — a corrupt
    body must never raise into the startup path."""
    try:
        raw = Path(lock_path).read_text().strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _write_pid(fd: int, pid: int) -> None:
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, str(pid).encode())
    os.fsync(fd)


def acquire(lock_path: Path) -> LockResult:
    """Take the singleton lock. On success (flock free, possibly reclaiming a dead holder's
    body) the returned fd MUST be kept referenced for the process lifetime. On contention with a
    live holder, acquired=False with truthful holder info and fd=None — the caller logs INFO and
    exits 0."""
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    prior_pid = read_holder_pid(path)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        holder = read_holder_pid(path)
        return LockResult(
            acquired=False,
            holder_pid=holder,
            holder_alive=is_pid_alive(holder),
            reclaimed=False,
            fd=None,
        )
    reclaimed = prior_pid is not None and prior_pid != os.getpid() and not is_pid_alive(prior_pid)
    _write_pid(fd, os.getpid())
    return LockResult(
        acquired=True,
        holder_pid=os.getpid(),
        holder_alive=True,
        reclaimed=reclaimed,
        fd=fd,
    )


def clear_holder(lock_path: Path) -> bool:
    """On a clean shutdown, empty the lock body — but only if OUR pid is the recorded holder, so
    we never clear a lock another live process holds. Returns True only when we cleared our own.

    Precondition: the caller still holds the flock (true at a clean shutdown, before fd close),
    so no other process can rewrite the body between the ownership read and the truncate."""
    holder = read_holder_pid(lock_path)
    if holder is None or holder != os.getpid():
        return False
    try:
        with open(lock_path, "r+b") as f:
            f.truncate(0)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        return False
    return True
