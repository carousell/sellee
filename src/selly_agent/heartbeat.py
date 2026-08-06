"""Liveness heartbeat — a JSON file, not a DB row or an event.

Written once per scheduler tick so get_status / a healthcheck can read {ts, pid, created}
without opening a DB, and so it never spams the transcript store. Write failures are logged at debug
and never crash the loop: a heartbeat miss is a symptom to surface, never a reason to die.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from selly_agent import proc_tree

log = logging.getLogger(__name__)


def write(path: Path, pid: int | None = None) -> None:
    pid = os.getpid() if pid is None else pid
    # `created` is the pid's identity, not decoration: a pid on its own is reused, so a reader
    # deciding whether to kill the daemon needs to know it is the same process that wrote this.
    record = {"ts": time.time(), "pid": pid, "created": proc_tree.creation_time(pid)}
    try:
        Path(path).write_text(json.dumps(record), encoding="utf-8")
    except OSError as exc:
        log.debug("heartbeat write failed: %s", exc)


def read(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def age(path: Path, now: float | None = None) -> float | None:
    """Seconds since the last tick, or None when there is no readable heartbeat.

    None and a large number mean different things — never written versus written and gone
    quiet — so every reader gets both from one place rather than re-deriving it.
    """
    data = read(path)
    if not data or not isinstance(data.get("ts"), (int, float)):
        return None
    return max(0.0, (time.time() if now is None else now) - data["ts"])


def wait_fresh(path: Path, *, newer_than: float, timeout_sec: float, poll_sec: float = 0.5) -> bool:
    """Block until the heartbeat is stamped after `newer_than`, or the timeout runs out.

    Compared against a timestamp the caller took before starting the daemon, so a heartbeat left
    behind by a previous run can never be mistaken for this one coming up.
    """
    deadline = time.monotonic() + timeout_sec
    while True:
        data = read(path)
        if data and isinstance(data.get("ts"), (int, float)) and data["ts"] >= newer_than:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_sec)
