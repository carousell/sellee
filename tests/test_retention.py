"""Retention prune: age deletion, log truncation, and a prune.done event with counts."""

from __future__ import annotations

import time

from selly_agent import migrations, retention
from selly_agent.db import Database
from selly_agent.events import EventBus, EventStore


def _bus(tmp_path) -> EventBus:
    events_db = Database(tmp_path / "events.db")
    data_db = Database(tmp_path / "selly.db")
    migrations.run_startup_migrations(
        data_db=data_db,
        events_db=events_db,
        backups_dir=tmp_path / "backups",
        backups_keep=5,
    )
    return EventBus(EventStore(events_db))


def test_run_retention_prunes_events_and_reports(tmp_path) -> None:
    bus = _bus(tmp_path)
    bus.publish("task.start", {})
    bus.publish("task.ok", {})
    (tmp_path / "backups").mkdir(exist_ok=True)
    (tmp_path / "logs").mkdir()

    counts = retention.run_retention(
        bus=bus,
        retention_days=1,
        routine_events_retention_hours=24,
        backups_dir=tmp_path / "backups",
        backups_keep=5,
        logs_dir=tmp_path / "logs",
        now=time.time() + 10 * retention.SECONDS_PER_DAY,
    )
    assert counts["events_deleted"] == 2
    # the prune.done event itself survives (published after the delete)
    kinds = [e.kind for e in bus.store.read()]
    assert kinds == ["prune.done"]


def test_run_retention_keeps_recent_events(tmp_path) -> None:
    bus = _bus(tmp_path)
    bus.publish("task.start", {})
    (tmp_path / "logs").mkdir()
    counts = retention.run_retention(
        bus=bus,
        retention_days=14,
        routine_events_retention_hours=24,
        backups_dir=tmp_path / "backups",
        backups_keep=5,
        logs_dir=tmp_path / "logs",
    )
    assert counts["events_deleted"] == 0


def _run(bus, tmp_path, *, retention_days=14, routine_hours=24, now=None):
    (tmp_path / "logs").mkdir(exist_ok=True)
    return retention.run_retention(
        bus=bus,
        retention_days=retention_days,
        routine_events_retention_hours=routine_hours,
        backups_dir=tmp_path / "backups",
        backups_keep=5,
        logs_dir=tmp_path / "logs",
        now=now,
    )


def test_routine_aged_out_while_info_retained(tmp_path) -> None:
    # A routine-tier heartbeat past its short window is pruned; a same-age info event, well
    # within retention_days, survives.
    bus = _bus(tmp_path)
    bus.publish("task.ok", {})  # routine
    bus.publish("channel.in", {})  # info
    counts = _run(bus, tmp_path, now=time.time() + 25 * retention.SECONDS_PER_HOUR)
    assert counts["routine_deleted"] == 1
    assert counts["events_deleted"] == 1  # no double-count with the main pass
    kinds = [e.kind for e in bus.store.read()]
    assert "channel.in" in kinds
    assert "task.ok" not in kinds


def test_routine_within_window_kept(tmp_path) -> None:
    bus = _bus(tmp_path)
    bus.publish("task.ok", {})
    counts = _run(bus, tmp_path, now=time.time() + 23 * retention.SECONDS_PER_HOUR)
    assert counts["routine_deleted"] == 0
    assert counts["events_deleted"] == 0


def test_info_aged_out_at_main_window_but_pass_end_kept(tmp_path) -> None:
    # Past retention_days the main pass prunes info events, but pass.end (KEEP_KINDS) survives so
    # a pass's final outcome outlives its verbose per-line events.
    bus = _bus(tmp_path)
    bus.publish("channel.in", {})  # info
    bus.publish("pass.end", {})  # info, but kept
    counts = _run(bus, tmp_path, now=time.time() + 15 * retention.SECONDS_PER_DAY)
    assert counts["routine_deleted"] == 0
    assert counts["events_deleted"] == 1
    kinds = [e.kind for e in bus.store.read()]
    assert "pass.end" in kinds
    assert "channel.in" not in kinds


def test_truncate_log_trims_to_cap(tmp_path) -> None:
    log_file = tmp_path / "agent.err.log"
    log_file.write_bytes(b"A" * 500 + b"B" * 500)
    reclaimed = retention._truncate_log(log_file, cap=200)
    assert reclaimed == 800
    assert log_file.read_bytes() == b"B" * 200


def test_truncate_log_leaves_small_files(tmp_path) -> None:
    log_file = tmp_path / "small.log"
    log_file.write_bytes(b"tiny")
    assert retention._truncate_log(log_file, cap=1024) == 0
    assert log_file.read_bytes() == b"tiny"
