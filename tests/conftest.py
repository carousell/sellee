"""Shared fixtures: an isolated XDG environment pointed at a tmpdir."""

from __future__ import annotations

import json
import time

import pytest

from selly_agent import migrations
from selly_agent.config import Config
from selly_agent.db import Database
from selly_agent.events import EventBus, EventStore


def seed_setting(store, key, value) -> None:
    """Write a setting directly, bypassing the change protocol — a test hook for arranging state
    (e.g. disabling quiet hours so a pacing-gated tool isn't blocked by the wall-clock hour a test
    happens to run in). Not a production path; real writes go through the propose→apply doors."""
    with store._db.transaction() as conn:  # noqa: SLF001 — tests may arrange store state directly
        conn.execute(
            "INSERT INTO settings (key, value, updated_ts) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
            "updated_ts = excluded.updated_ts",
            (key, json.dumps(value), time.time()),
        )


@pytest.fixture
def bus(tmp_path):
    """A ready EventBus backed by freshly-migrated data/events DBs under tmp_path."""
    data_db = Database(tmp_path / "selly.db")
    events_db = Database(tmp_path / "events.db")
    migrations.run_startup_migrations(
        data_db=data_db,
        events_db=events_db,
        backups_dir=tmp_path / "backups",
        backups_keep=5,
    )
    return EventBus(EventStore(events_db))


@pytest.fixture
def store(bus):
    """A Store over the same freshly-migrated selly.db the bus fixture created. Quiet hours are
    seeded off so a pacing-gated tool isn't blocked by the wall-clock hour a test runs in (quiet
    hours moved from a config knob to a setting); a settings-behavior test that needs the registry
    default or a specific window builds its own store or re-seeds."""
    from selly_agent.store import Store

    st = Store(Database(bus.store.db.path.parent / "selly.db"))
    seed_setting(st, "quiet_hours", [0, 0])
    return st


@pytest.fixture
def fresh_store(tmp_path):
    """A migrated Store with nothing seeded — for settings tests that need the registry default
    (the `store` fixture seeds quiet hours off)."""
    from selly_agent.store import Store

    data_db = Database(tmp_path / "fresh.db")
    events_db = Database(tmp_path / "fresh-events.db")
    migrations.run_startup_migrations(
        data_db=data_db, events_db=events_db, backups_dir=tmp_path / "fresh-backups", backups_keep=5
    )
    return Store(data_db)


@pytest.fixture
def make_ctx(bus, store, xdg_tmp):
    """Factory for a ToolContext, so tests pick the tier / pass id / rail per case.

    Depends on xdg_tmp so secret and heartbeat reads inside handlers stay hermetic.
    """
    from selly_agent.store import ScopedStore
    from selly_agent.tools.registry import Session, ToolContext

    def _make(
        tier,
        *,
        pass_id=None,
        scope=None,
        rail_factory=None,
        reply_sink=None,
        config=None,
        started_ts=1000.0,
    ):
        # A scoped session sees a ScopedStore (as the daemon builds per request); unscoped uses the
        # raw store. The store fixture seeds quiet hours off, so a pacing-gated tool (publish,
        # send_reply) is not blocked by the wall-clock hour a test happens to run in.
        return ToolContext(
            session=Session(tier=tier, pass_id=pass_id, scope=scope),
            store=ScopedStore(store, scope) if scope is not None else store,
            bus=bus,
            config=config or Config(),
            rail_factory=rail_factory,
            reply_sink=reply_sink,
            started_ts=started_ts,
        )

    return _make


@pytest.fixture
def xdg_tmp(tmp_path, monkeypatch):
    """Point HOME and every XDG_*_HOME at a fresh tmpdir so path resolution is hermetic."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path
