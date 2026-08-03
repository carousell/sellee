"""Event store + bus: ts stamped at write, filters, subscribers, age/keep-kind pruning."""

from __future__ import annotations

import time

from selly_agent import migrations
from selly_agent.db import Database, connect_reader
from selly_agent.events import EventBus, EventStore, latest_seq, routine_kinds


def _store(tmp_path) -> EventStore:
    events_db = Database(tmp_path / "events.db")
    data_db = Database(tmp_path / "selly.db")
    migrations.run_startup_migrations(
        data_db=data_db,
        events_db=events_db,
        backups_dir=tmp_path / "backups",
        backups_keep=5,
    )
    return EventStore(events_db)


def test_ts_is_stamped_at_write_not_by_caller(tmp_path) -> None:
    store = _store(tmp_path)
    before = time.time()
    # a transport clock in the payload must not become the ordering timestamp
    event = store.record("task.start", {"ts": 0, "src_ts": 111}, pass_id=None)
    after = time.time()
    assert before <= event.ts <= after
    (row,) = store.read()
    assert row.ts == event.ts
    assert row.payload["src_ts"] == 111  # payload preserved verbatim


def test_seq_orders_and_pass_id_filters(tmp_path) -> None:
    store = _store(tmp_path)
    store.record("task.start", {}, pass_id="p1")
    store.record("task.ok", {}, pass_id="p1")
    store.record("task.start", {}, pass_id="p2")

    seqs = [e.seq for e in store.read()]
    assert seqs == sorted(seqs)
    assert [e.kind for e in store.read(pass_id="p1")] == ["task.start", "task.ok"]
    assert [e.kind for e in store.read(kinds=["task.start"])] == ["task.start", "task.start"]

    last = seqs[0]
    assert [e.seq for e in store.read(after_seq=last)] == seqs[1:]


def test_bus_publishes_to_store_and_subscribers(tmp_path) -> None:
    bus = EventBus(_store(tmp_path))
    seen = []
    unsubscribe = bus.subscribe(seen.append)

    ev = bus.publish("daemon.start", {"pid": 42})
    assert [e.seq for e in seen] == [ev.seq]
    assert bus.store.read()[-1].kind == "daemon.start"

    unsubscribe()
    bus.publish("daemon.stop", {})
    assert len(seen) == 1  # no longer receiving after unsubscribe


def test_broken_subscriber_does_not_break_publish(tmp_path) -> None:
    bus = EventBus(_store(tmp_path))

    def boom(_event):
        raise RuntimeError("subscriber failure")

    bus.subscribe(boom)
    ev = bus.publish("task.ok", {})  # must not raise
    assert bus.store.read(after_seq=ev.seq - 1)[0].kind == "task.ok"


def test_delete_older_than_honors_cutoff_and_keep_kinds(tmp_path) -> None:
    store = _store(tmp_path)
    store.record("task.error", {}, pass_id=None)
    store.record("task.error", {}, pass_id=None)
    keeper = store.record("prune.done", {}, pass_id=None)

    # a cutoff in the past deletes nothing (all rows are newer)
    assert store.delete_older_than(0.0, keep_kinds=()) == 0

    # a cutoff in the future deletes everything except the keep-kind
    deleted = store.delete_older_than(time.time() + 1000, keep_kinds=("prune.done",))
    assert deleted == 2
    remaining = store.read()
    assert [e.seq for e in remaining] == [keeper.seq]


def test_routine_kinds_are_the_demoted_ones(tmp_path) -> None:
    # Derived from the level map, so it's exactly the kinds retention ages out on the short
    # window — task.start/task.ok today, and whatever else gets demoted later.
    assert set(routine_kinds()) == {"task.start", "task.ok"}


def test_delete_kinds_older_than_targets_only_listed_kinds(tmp_path) -> None:
    store = _store(tmp_path)
    store.record("task.ok", {}, pass_id=None)  # routine, in the target set
    keeper = store.record("channel.in", {}, pass_id=None)  # info, not in the set

    # a past cutoff deletes nothing; an empty kinds set is a no-op even with a future cutoff
    assert store.delete_kinds_older_than(0.0, ("task.ok",)) == 0
    assert store.delete_kinds_older_than(time.time() + 1000, ()) == 0

    deleted = store.delete_kinds_older_than(time.time() + 1000, ("task.ok",))
    assert deleted == 1
    assert [e.seq for e in store.read()] == [keeper.seq]  # the info event is untouched


def test_newest_takes_the_last_rows_but_answers_oldest_first(tmp_path) -> None:
    """What lets a tail open at now: the newest page, still in reading order."""
    store = _store(tmp_path)
    for i in range(10):
        store.record("demo.event", {"i": i}, pass_id=None)

    assert [e.payload["i"] for e in store.read(limit=3, newest=True)] == [7, 8, 9]
    assert [e.payload["i"] for e in store.read(limit=3)] == [0, 1, 2]


def test_exclude_kinds_drops_a_whole_tier(tmp_path) -> None:
    store = _store(tmp_path)
    store.record("task.ok", {}, pass_id=None)
    store.record("demo.event", {}, pass_id=None)

    assert [e.kind for e in store.read(exclude_kinds=routine_kinds())] == ["demo.event"]
    # an empty exclusion is a no-op, not "exclude everything"
    assert len(store.read(exclude_kinds=())) == 2


def test_upto_seq_bounds_the_read_at_a_ceiling(tmp_path) -> None:
    """Read against a ceiling taken before the rows, so an event arriving mid-request is left for
    the next read rather than skipped by a cursor that jumped over it."""
    store = _store(tmp_path)
    first = store.record("demo.event", {"i": 0}, pass_id=None)
    ceiling = latest_seq(connect_reader(tmp_path / "events.db"))
    store.record("demo.event", {"i": 1}, pass_id=None)  # lands after the ceiling was read

    assert [e.seq for e in store.read(upto_seq=ceiling)] == [first.seq]
    assert [e.payload["i"] for e in store.read(after_seq=ceiling)] == [1]


def test_latest_seq_is_zero_on_an_empty_store(tmp_path) -> None:
    store = _store(tmp_path)
    assert latest_seq(connect_reader(tmp_path / "events.db")) == 0
    recorded = store.record("demo.event", {}, pass_id=None)
    assert latest_seq(connect_reader(tmp_path / "events.db")) == recorded.seq
