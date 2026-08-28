"""Migration runner: fresh apply, partial pending, rollback-on-failure, snapshots, recreate."""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from sellee import migrations, store
from sellee.db import Database
from sellee.store.helpers import _channel_from_row


def _table_exists(db: Database, name: str) -> bool:
    rows = db.query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return bool(rows)


def _make_dbs(tmp_path):
    data_db = Database(tmp_path / "data" / "sellee.db")
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
        ("data", 11),
        ("data", 12),
        ("data", 13),
        ("data", 14),
        ("data", 15),
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
    assert _table_exists(data_db, "market_connect_requests")
    assert _table_exists(data_db, "market_surveys")
    assert _table_exists(data_db, "discovered_listings")
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
        11,
        12,
        13,
        14,
        15,
    }
    assert {r["version"] for r in events_db.query("SELECT version FROM schema_migrations")} == {1}


def test_second_run_applies_nothing_and_writes_no_snapshot(tmp_path) -> None:
    data_db, events_db = _make_dbs(tmp_path)
    _run(tmp_path, data_db, events_db)
    backups_dir = tmp_path / "state" / "backups"
    after_first = sorted(backups_dir.glob("sellee-*.db"))

    applied = _run(tmp_path, data_db, events_db)
    assert applied == []
    assert sorted(backups_dir.glob("sellee-*.db")) == after_first


def test_events_db_recreated_after_deletion(tmp_path) -> None:
    data_db, events_db = _make_dbs(tmp_path)
    _run(tmp_path, data_db, events_db)

    events_db.close()
    events_db.path.unlink()

    events_db2 = Database(events_db.path)
    _run(tmp_path, data_db, events_db2)
    assert _table_exists(events_db2, "events")


def test_channel_adapter_column_takes_any_non_empty_name(tmp_path) -> None:
    """The adapter column carries no enumerating CHECK, only a non-empty one: adding a third
    provider is a code change (store.KNOWN_ADAPTERS), not another recreate-copy-swap of the whole
    table — which is the only way SQLite can widen a CHECK."""
    data_db, events_db = _make_dbs(tmp_path)
    _run(tmp_path, data_db, events_db)

    with data_db.transaction() as conn:
        conn.execute("INSERT INTO channel (id, adapter, updated_ts) VALUES (1, 'matrix', 1.0)")
    assert data_db.query("SELECT adapter FROM channel")[0]["adapter"] == "matrix"

    with pytest.raises(sqlite3.IntegrityError), data_db.transaction() as conn:
        conn.execute("UPDATE channel SET adapter = '' WHERE id = 1")


def test_0010_preserves_the_existing_channel_row(tmp_path, monkeypatch) -> None:
    """0010 recreates the channel table, so the singleton row has to survive the copy: a returning
    seller's bound channel — cursor, greeting stamp and all — must not be reset by an upgrade.
    Driven by applying the real migrations with 0010 withheld, binding a channel, then letting
    0010 land on top of it. 0011 is withheld with it: it adds a column to the table 0010 recreates,
    so applying them out of order would leave a schema no real install ever has."""
    real_data = migrations._MIGRATIONS_ROOT / "data"
    root = tmp_path / "migrations"
    (root / "data").mkdir(parents=True)
    (root / "events").mkdir(parents=True)
    for src in sorted(real_data.glob("*.sql")):
        if not src.name.startswith(("0010", "0011")):
            shutil.copy(src, root / "data" / src.name)
    for src in (migrations._MIGRATIONS_ROOT / "events").glob("*.sql"):
        shutil.copy(src, root / "events" / src.name)
    monkeypatch.setattr(migrations, "_MIGRATIONS_ROOT", root)

    data_db, events_db = _make_dbs(tmp_path)
    _run(tmp_path, data_db, events_db)
    with data_db.transaction() as conn:
        conn.execute(
            "INSERT INTO channel (id, adapter, bot_username, chat_id, update_offset, bind_nonce,"
            " welcomed_at, commands_hash, bound_ts, updated_ts)"
            " VALUES (1, 'telegram', 'sellee_bot', 555, 7, NULL, 100.0, 'abc123', 90.0, 110.0)"
        )

    for name in ("0010_discord_channel.sql", "0011_channel_nonce_ttl.sql"):
        shutil.copy(real_data / name, root / "data" / name)
    applied = _run(tmp_path, data_db, events_db)

    assert [(a.db, a.version) for a in applied] == [("data", 10), ("data", 11)]
    row = data_db.query("SELECT * FROM channel WHERE id = 1")[0]
    assert row["adapter"] == "telegram"
    assert row["bot_username"] == "sellee_bot"
    assert row["chat_id"] == 555
    assert row["update_offset"] == 7
    assert row["welcomed_at"] == 100.0
    assert row["commands_hash"] == "abc123"
    assert row["bound_ts"] == 90.0


def test_0011_leaves_an_upgraded_rows_nonce_without_a_deadline(tmp_path, monkeypatch) -> None:
    """A nonce armed before the TTL existed gets no expiry from the upgrade — the column is
    nullable and NULL, which the store reads as already expired. Failing closed is the point: those
    are the unbounded nonces the column was added to retire."""
    real_data = migrations._MIGRATIONS_ROOT / "data"
    root = tmp_path / "migrations"
    (root / "data").mkdir(parents=True)
    (root / "events").mkdir(parents=True)
    for src in sorted(real_data.glob("*.sql")):
        if not src.name.startswith("0011"):
            shutil.copy(src, root / "data" / src.name)
    for src in (migrations._MIGRATIONS_ROOT / "events").glob("*.sql"):
        shutil.copy(src, root / "events" / src.name)
    monkeypatch.setattr(migrations, "_MIGRATIONS_ROOT", root)

    data_db, events_db = _make_dbs(tmp_path)
    _run(tmp_path, data_db, events_db)
    with data_db.transaction() as conn:
        conn.execute(
            "INSERT INTO channel (id, adapter, bot_username, bind_nonce, updated_ts)"
            " VALUES (1, 'telegram', 'sellee_bot', 'armed-long-ago', 110.0)"
        )

    ttl_sql = "0011_channel_nonce_ttl.sql"
    shutil.copy(real_data / ttl_sql, root / "data" / ttl_sql)
    _run(tmp_path, data_db, events_db)

    row = data_db.query("SELECT * FROM channel WHERE id = 1")[0]
    assert row["bind_nonce"] == "armed-long-ago"  # the row survives the ALTER
    assert row["bind_nonce_expires_ts"] is None
    assert store.bind_nonce_live(_channel_from_row(row)) is False


def test_0015_starts_existing_rows_unchecked_and_unrestorable(tmp_path, monkeypatch) -> None:
    """An intent and a thread that predate the self-settling loop must upgrade to the safe reading:
    verify_attempts 0 means "no lane has looked yet" (so the sweep's ceiling still covers it), and a
    NULL escalated_from_status means "we do not know what to restore" rather than a guessed
    `active`, which on an `agreed` thread would re-open a closed deal."""
    real_data = migrations._MIGRATIONS_ROOT / "data"
    root = tmp_path / "migrations"
    (root / "data").mkdir(parents=True)
    (root / "events").mkdir(parents=True)
    for src in sorted(real_data.glob("*.sql")):
        if not src.name.startswith("0015"):
            shutil.copy(src, root / "data" / src.name)
    for src in (migrations._MIGRATIONS_ROOT / "events").glob("*.sql"):
        shutil.copy(src, root / "events" / src.name)
    monkeypatch.setattr(migrations, "_MIGRATIONS_ROOT", root)

    data_db, events_db = _make_dbs(tmp_path)
    _run(tmp_path, data_db, events_db)
    with data_db.transaction() as conn:
        conn.execute(
            "INSERT INTO items (id, title, status, created_ts, updated_ts)"
            " VALUES ('item_x', 'Teak lamp', 'ready', 100.0, 100.0)"
        )
        conn.execute(
            "INSERT INTO threads (thread_id, side, market, item_id, counterpart_handle, status,"
            " created_ts, updated_ts) VALUES ('carousell:1', 'sell', 'carousell', 'item_x',"
            " 'bob', 'agreed', 100.0, 100.0)"
        )
        conn.execute(
            "INSERT INTO send_intents (intent_id, thread_id, text, kind, status, created_ts)"
            " VALUES ('intent_x', 'carousell:1', 'on its way!', 'reply', 'sent_unverified', 100.0)"
        )

    verify_sql = "0015_intent_verify.sql"
    shutil.copy(real_data / verify_sql, root / "data" / verify_sql)
    _run(tmp_path, data_db, events_db)

    intent = data_db.query("SELECT * FROM send_intents WHERE intent_id = 'intent_x'")[0]
    assert intent["status"] == "sent_unverified"  # the row survives the ALTER
    assert intent["verify_attempts"] == 0
    thread = data_db.query("SELECT * FROM threads WHERE thread_id = 'carousell:1'")[0]
    assert thread["status"] == "agreed"
    assert thread["escalated_from_status"] is None


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
    assert len(list(backups_dir.glob("sellee-*.db"))) == 1  # pre-0001

    # events-only pending must NOT snapshot sellee.db
    custom_root("events", "0002_more.sql", "CREATE TABLE ev2 (x);")
    _run(tmp_path, data_db, events_db)
    assert len(list(backups_dir.glob("sellee-*.db"))) == 1


def test_snapshot_pruning_keeps_newest(tmp_path) -> None:
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    for i in range(1, 6):
        f = backups_dir / f"sellee-100000000{i}-pre-{i:04d}.db"
        f.write_bytes(b"x")
    migrations.prune_backups(backups_dir, backups_keep=2)
    remaining = {p.name for p in backups_dir.glob("sellee-*.db")}
    assert remaining == {
        "sellee-1000000005-pre-0005.db",
        "sellee-1000000004-pre-0004.db",
    }
