"""Shared fixtures: an isolated XDG environment pointed at a tmpdir."""

from __future__ import annotations

import json
import re
import time

import pytest

from selly_agent import migrations, paths
from selly_agent.config import Config
from selly_agent.db import Database
from selly_agent.events import EventBus, EventStore


def leak_paths(node, sentinel, path="$") -> list:
    """Where a sentinel value appears inside a payload, walking its structure — [] is clean.

    The leak sweeps used to scan str(payload) for the sentinel's digits, which trips on digits
    that happen to fall inside a random id ("call_47331f…" contains 7331). So numbers are compared
    as numbers, and strings are searched for the sentinel standing alone — never mid-token.
    """
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            found += leak_paths(value, sentinel, f"{path}.{key}")
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            found += leak_paths(value, sentinel, f"{path}[{index}]")
    elif isinstance(node, bool) or node is None:
        pass
    elif isinstance(node, (int, float)):
        if not isinstance(sentinel, str) and float(node) == float(sentinel):
            found.append(path)
    elif isinstance(node, str):
        if _sentinel_pattern(sentinel).search(node):
            found.append(path)
    return found


def _sentinel_pattern(sentinel) -> re.Pattern:
    if isinstance(sentinel, str):
        return re.compile(re.escape(sentinel))
    digits = re.escape(f"{sentinel:g}")  # 61.0 -> "61"
    return re.compile(rf"(?<![0-9A-Za-z_]){digits}(\.0+)?(?![0-9A-Za-z_])")


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
        browser_factory=None,
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
            # The daemon hands tools a factory (acquiring is what starts Chrome); tests hand in a
            # built fake, so wrap it the way the tool will call it.
            reply_sink=(lambda: reply_sink) if reply_sink is not None else None,
            browser_factory=browser_factory,
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


@pytest.fixture
def container(monkeypatch):
    """Run the test as if inside the container image, whose Dockerfile sets this marker."""
    from selly_agent import deployment

    monkeypatch.setenv(deployment.MARKER_VAR, deployment.CONTAINER)


@pytest.fixture
def tree(tmp_path):
    """A minimal selly-agent tree: what a checkout or an unpacked release looks like.

    Lives here rather than in each installer test module because it has to stay in step with
    what a version directory is required to contain — miss a file and staging produces a version
    that cannot build its own dependencies.
    """
    root = tmp_path / "checkout"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "selly-agent").write_text("#!/usr/bin/env python3\n")
    (root / "src" / "selly_agent" / "__pycache__").mkdir(parents=True)
    (root / "src" / "selly_agent" / "__init__.py").write_text("__version__ = '9.9.9'\n")
    (root / "src" / "selly_agent" / "__pycache__" / "stale.pyc").write_text("junk")
    (root / "README.md").write_text("docs\n")
    (root / "pyproject.toml").write_text("[project]\nname = 'selly-agent'\n")
    (root / "uv.lock").write_text("version = 1\n")
    (root / ".python-version").write_text("3.14\n")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/master\n")
    # A dev checkout has its own venv, holding absolute paths that would be wrong once copied.
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    return root


@pytest.fixture(autouse=True)
def stub_provision(monkeypatch):
    """Stand in for building a version's venv.

    Autouse so no test can accidentally download an interpreter and a wheel set: provisioning
    against real uv belongs in tests/integration. Records what it was asked to provision.
    """
    from selly_agent.installer import runtime

    provisioned = []

    def fake(dest, **kwargs):
        provisioned.append(dest)
        interpreter = paths.venv_python(dest)
        interpreter.parent.mkdir(parents=True, exist_ok=True)
        interpreter.write_text("#!/bin/sh\n")
        return interpreter

    monkeypatch.setattr(runtime, "provision", fake)
    return provisioned
