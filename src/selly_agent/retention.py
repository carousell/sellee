"""Retention prune — the daily housekeeping task (A11).

State is prunable by definition: full events are kept for a window, DB snapshots to a count,
and the launchd-redirected logs (which never rotate on their own) are trimmed to a size cap.
Events are the durable record, not log lines, so trimming logs loses nothing that matters.
No VACUUM — WAL checkpointing suffices at this volume.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from selly_agent import migrations
from selly_agent.events import EventBus

log = logging.getLogger(__name__)

SECONDS_PER_DAY = 86400

# Kinds never pruned by age: the kept pass summaries, so the store keeps "full events for N
# days, summaries kept" — a pass's final outcome survives a prune even when its verbose
# per-line events age out.
KEEP_KINDS: tuple[str, ...] = ("pass.end",)

# Per-file cap for the launchd-redirected logs. The redirect never rotates, so unbounded logs
# were exactly the kind of state/ growth the layout makes prunable.
LOG_SIZE_CAP_BYTES = 10 * 1024 * 1024


def _truncate_log(path: Path, cap: int) -> int:
    """Trim a log to its last `cap` bytes, returning bytes reclaimed. Best-effort: any IO error
    is swallowed (housekeeping must never crash the loop)."""
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size <= cap:
        return 0
    try:
        with open(path, "rb") as f:
            f.seek(size - cap)
            tail = f.read()
        with open(path, "wb") as f:
            f.write(tail)
    except OSError as exc:
        log.warning("could not truncate log %s: %s", path, exc)
        return 0
    return size - len(tail)


def run_retention(
    *,
    bus: EventBus,
    retention_days: int,
    backups_dir: Path,
    backups_keep: int,
    logs_dir: Path,
    log_size_cap: int = LOG_SIZE_CAP_BYTES,
    now: float | None = None,
) -> dict:
    """Prune events past the retention window (except keep-kinds), prune DB snapshots to the
    keep count, and trim logs to the size cap. Publishes a prune.done event with the counts."""
    now = time.time() if now is None else now
    cutoff = now - retention_days * SECONDS_PER_DAY

    events_deleted = bus.store.delete_older_than(cutoff, KEEP_KINDS)

    before = len(list(backups_dir.glob("selly-*.db"))) if backups_dir.is_dir() else 0
    if backups_dir.is_dir():
        migrations.prune_backups(backups_dir, backups_keep)
    after = len(list(backups_dir.glob("selly-*.db"))) if backups_dir.is_dir() else 0

    log_bytes = 0
    if logs_dir.is_dir():
        for entry in sorted(logs_dir.glob("*")):
            if entry.is_file():
                log_bytes += _truncate_log(entry, log_size_cap)

    counts = {
        "events_deleted": events_deleted,
        "backups_pruned": before - after,
        "log_bytes_reclaimed": log_bytes,
    }
    bus.publish("prune.done", counts)
    return counts
