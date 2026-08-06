"""Migration runner: fresh apply, partial pending, rollback-on-failure, snapshots, recreate."""

from __future__ import annotations

import sqlite3

import pytest

from selly_agent import migrations
from selly_agent.db import Database


def _table_exists(db: Database, name: str) -> bool:
    rows = db.query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return bool(rows)


def _make_dbs(tmp_path):
    data_db = Database(tmp_path / "data" / "selly.db")
    events_db = Database(tmp_path / "state" / "events.db")
    return data_db, events_db


def _run(tmp_path, data_db, events_db, backups_keep=5):
    return migrations.run_startup_migrations(
        data_db=data_db,
        events_db=events_db,
        backups_dir=tmp_path / "state" / "backups",
        backups_keep=backups_keep,
    )


# --- against the real shipped migrations ---------------------------------------------------


def test_fresh_apply_creates_both_schemas(tmp_path) -> None:
    data_db, events_db = _make_dbs(tmp_path)
    applied = _run(tmp_path, data_db, events_db)

    assert {(a.db, a.version) for a in applied} == {
        ("data", 1),
        ("data", 2),
        ("data", 3),
        ("data", 4),
        ("data", 5),
        ("data", 6),
        ("data", 7),
        ("data", 8),
        ("data", 9),
        ("data", 10),
        ("events", 1),
    }
    assert _table_exists(data_db, "meta")
    assert _table_exists(data_db, "items")
    assert _table_exists(data_db, "floors")
    assert _table_exists(data_db, "passes")
    assert _table_exists(data_db, "threads")
    assert _table_exists(data_db, "wants")
    assert _table_exists(data_db, "budgets")
    assert _table_exists(data_db, "channel")
    assert _table_exists(data_db, "channel_inbox")
    assert _table_exists(data_db, "notices")
    assert _table_exists(data_db, "control")
    assert _table_exists(data_db, "settings")
    assert _table_exists(data_db, "pending_setting_changes")
    assert _table_exists(data_db, "qa_bank")
    assert _table_exists(data_db, "ui_cache")
    assert _table_exists(data_db, "crosslink_pushes")
    assert _table_exists(data_db, "pass_processes")
    assert _table_exists(events_db, "events")
    assert {r["version"] for r in data_db.query("SELECT version FROM schema_migrations")} == {
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
    }
    assert {r["version"] for r in events_db.query("SELECT version FROM schema_migrations")} == {1}


def test_second_run_applies_nothing_and_writes_no_snapshot(tmp_path) -> None:
    data_db, events_db = _make_dbs(tmp_path)
    _run(tmp_path, data_db, events_db)
    backups_dir = tmp_path / "state" / "backups"
    after_first = sorted(backups_dir.glob("selly-*.db"))

    applied = _run(tmp_path, data_db, events_db)
    assert applied == []
    assert sorted(backups_dir.glob("selly-*.db")) == after_first


def test_events_db_recreated_after_deletion(tmp_path) -> None:
    data_db, events_db = _make_dbs(tmp_path)
    _run(tmp_path, data_db, events_db)

    events_db.close()
    events_db.path.unlink()

    events_db2 = Database(events_db.path)
    _run(tmp_path, data_db, events_db2)
    assert _table_exists(events_db2, "events")


# --- against synthetic migration sets ------------------------------------------------------


@pytest.fixture
def custom_root(tmp_path, monkeypatch):
    root = tmp_path / "migrations"
    (root / "data").mkdir(parents=True)
    (root / "events").mkdir(parents=True)
    monkeypatch.setattr(migrations, "_MIGRATIONS_ROOT", root)

    def write(db_name: str, filename: str, sql: str) -> None:
        (root / db_name / filename).write_text(sql)

    # a minimal events schema so run_startup_migrations has something valid to apply there
    write("events", "0001_init.sql", "CREATE TABLE ev (seq INTEGER PRIMARY KEY);")
    return write


def test_partial_pending_applies_only_new_versions(tmp_path, custom_root) -> None:
    custom_root("data", "0001_a.sql", "CREATE TABLE a (x);")
    data_db, events_db = _make_dbs(tmp_path)
    _run(tmp_path, data_db, events_db)

    custom_root("data", "0002_b.sql", "CREATE TABLE b (x);")
    applied = _run(tmp_path, data_db, events_db)

    assert [(a.db, a.version) for a in applied] == [("data", 2)]
    assert _table_exists(data_db, "a")
    assert _table_exists(data_db, "b")


def test_failure_mid_file_rolls_back_and_aborts(tmp_path, custom_root) -> None:
    custom_root("data", "0001_ok.sql", "CREATE TABLE ok (x);")
    data_db, events_db = _make_dbs(tmp_path)
    _run(tmp_path, data_db, events_db)

    # first statement would succeed, second collides with an existing table -> whole file aborts
    custom_root(
        "data",
        "0002_bad.sql",
        "CREATE TABLE mid (x);\nCREATE TABLE ok (x);\n",
    )
    with pytest.raises(sqlite3.OperationalError):
        _run(tmp_path, data_db, events_db)

    assert not _table_exists(data_db, "mid")  # rolled back
    assert {r["version"] for r in data_db.query("SELECT version FROM schema_migrations")} == {1}


def test_snapshot_only_created_when_data_pending(tmp_path, custom_root) -> None:
    custom_root("data", "0001_a.sql", "CREATE TABLE a (x);")
    data_db, events_db = _make_dbs(tmp_path)
    _run(tmp_path, data_db, events_db)
    backups_dir = tmp_path / "state" / "backups"
    assert len(list(backups_dir.glob("selly-*.db"))) == 1  # pre-0001

    # events-only pending must NOT snapshot selly.db
    custom_root("events", "0002_more.sql", "CREATE TABLE ev2 (x);")
    _run(tmp_path, data_db, events_db)
    assert len(list(backups_dir.glob("selly-*.db"))) == 1


def test_snapshot_pruning_keeps_newest(tmp_path) -> None:
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    for i in range(1, 6):
        f = backups_dir / f"selly-100000000{i}-pre-{i:04d}.db"
        f.write_bytes(b"x")
    migrations.prune_backups(backups_dir, backups_keep=2)
    remaining = {p.name for p in backups_dir.glob("selly-*.db")}
    assert remaining == {
        "selly-1000000005-pre-0005.db",
        "selly-1000000004-pre-0004.db",
    }
