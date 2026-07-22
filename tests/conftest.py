"""Shared fixtures: an isolated XDG environment pointed at a tmpdir."""

from __future__ import annotations

import pytest

from selly_agent import migrations
from selly_agent.config import Config
from selly_agent.db import Database
from selly_agent.events import EventBus, EventStore


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
    """A Store over the same freshly-migrated selly.db the bus fixture created."""
    from selly_agent.store import Store

    return Store(Database(bus.store.db.path.parent / "selly.db"))


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
        # raw store. Default quiet hours off so a pacing-gated tool (publish, send_reply) is not
        # blocked by the wall-clock hour a test happens to run in — quiet-verdict tests pass config.
        return ToolContext(
            session=Session(tier=tier, pass_id=pass_id, scope=scope),
            store=ScopedStore(store, scope) if scope is not None else store,
            bus=bus,
            config=config or Config(quiet_hours=(0, 0)),
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
