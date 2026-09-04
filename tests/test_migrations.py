"""Migration runner: fresh apply, partial pending, rollback-on-failure, snapshots, recreate."""

from __future__ import annotations

import json
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
        ("data", 16),
        ("data", 17),
        ("data", 18),
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
    assert _table_exists(data_db, "browser_holds")
    assert _table_exists(data_db, "thread_listing_lookups")
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
        16,
        17,
        18,
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
    """The adapter column carries only a non-empty CHECK, not an enumeration: adding a third
    provider stays a code change, not a table recreate."""
    data_db, events_db = _make_dbs(tmp_path)
    _run(tmp_path, data_db, events_db)

    with data_db.transaction() as conn:
        conn.execute("INSERT INTO channel (id, adapter, updated_ts) VALUES (1, 'matrix', 1.0)")
    assert data_db.query("SELECT adapter FROM channel")[0]["adapter"] == "matrix"

    with pytest.raises(sqlite3.IntegrityError), data_db.transaction() as conn:
        conn.execute("UPDATE channel SET adapter = '' WHERE id = 1")


def test_0010_preserves_the_existing_channel_row(tmp_path, monkeypatch) -> None:
    """0010 recreates the channel table, so the singleton row must survive the copy. 0011 is
    withheld with it: it adds a column to the table 0010 recreates, so applying them out of order
    would leave a schema no real install ever has."""
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
    """A nonce armed before the TTL existed upgrades to NULL, which the store reads as already
    expired — failing closed is the point."""
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


def _withhold(tmp_path, monkeypatch, prefix):
    """Apply every shipped migration except the one under test, and answer where the real ones are:
    bring a database up to the release before a migration, then let that one land on top."""
    real_data = migrations._MIGRATIONS_ROOT / "data"
    root = tmp_path / "migrations"
    (root / "data").mkdir(parents=True)
    (root / "events").mkdir(parents=True)
    for src in sorted(real_data.glob("*.sql")):
        if not src.name.startswith(prefix):
            shutil.copy(src, root / "data" / src.name)
    for src in (migrations._MIGRATIONS_ROOT / "events").glob("*.sql"):
        shutil.copy(src, root / "events" / src.name)
    monkeypatch.setattr(migrations, "_MIGRATIONS_ROOT", root)
    return real_data, root


def _apply_0016(tmp_path, real_data, root, data_db, events_db):
    shutil.copy(real_data / "0016_connected_markets.sql", root / "data")
    _run(tmp_path, data_db, events_db)


def _connected(data_db):
    rows = data_db.query("SELECT value FROM settings WHERE key = 'connected_markets'")
    return json.loads(rows[0]["value"]) if rows else None


def _seed_read_thread(conn, thread_id="carousell:1", market="carousell", source="browser_read"):
    conn.execute(
        "INSERT OR IGNORE INTO items (id, title, status, created_ts, updated_ts)"
        " VALUES ('item_x', 'Teak lamp', 'ready', 100.0, 100.0)"
    )
    conn.execute(
        "INSERT INTO threads (thread_id, side, market, item_id, counterpart_handle, status,"
        " source, created_ts, updated_ts)"
        " VALUES (?, 'sell', ?, 'item_x', 'bob', 'active', ?, 100.0, 100.0)",
        (thread_id, market, source),
    )


def test_0016_moves_the_setting_and_its_whole_ledger(tmp_path, monkeypatch) -> None:
    """The ledger key has to move with the setting: `settings.decide` looks a change's spec up by
    the stored key, so a rename that touched only the settings row would break Approve and Undo."""
    real_data, root = _withhold(tmp_path, monkeypatch, "0016")
    data_db, events_db = _make_dbs(tmp_path)
    _run(tmp_path, data_db, events_db)
    with data_db.transaction() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, updated_ts) VALUES"
            " ('crosslist_markets', '[\"carousell\"]', 100.0)"
        )
        conn.execute(
            "INSERT INTO pending_setting_changes (change_id, key, value, status, proposed_ts)"
            " VALUES ('chg_pending', 'crosslist_markets', '[\"carousell\"]', 'pending', 100.0)"
        )
        conn.execute(
            "INSERT INTO pending_setting_changes (change_id, key, value, status, proposed_ts)"
            " VALUES ('chg_applied', 'crosslist_markets', '[\"carousell\"]', 'applied', 100.0)"
        )

    _apply_0016(tmp_path, real_data, root, data_db, events_db)

    assert _connected(data_db) == ["carousell"]
    assert data_db.query("SELECT 1 FROM settings WHERE key = 'crosslist_markets'") == []
    keys = {
        r["change_id"]: r["key"]
        for r in data_db.query("SELECT change_id, key FROM pending_setting_changes")
    }
    assert keys == {"chg_pending": "connected_markets", "chg_applied": "connected_markets"}


def test_0016_connects_a_market_that_was_being_read_with_the_setting_empty(
    tmp_path, monkeypatch
) -> None:
    """Reading was never gated by the setting, so a seller mid-conversation would be connected to
    nothing by a plain rename."""
    real_data, root = _withhold(tmp_path, monkeypatch, "0016")
    data_db, events_db = _make_dbs(tmp_path)
    _run(tmp_path, data_db, events_db)
    with data_db.transaction() as conn:
        _seed_read_thread(conn)

    _apply_0016(tmp_path, real_data, root, data_db, events_db)

    assert _connected(data_db) == ["carousell"]


def test_0016_unions_rather_than_replaces_a_cleared_list(tmp_path, monkeypatch) -> None:
    """A seller who turned cross-listing off still has their inbox read; the evidence is unioned
    in, because someone who wants it off can turn it off again but a stranded seller cannot."""
    real_data, root = _withhold(tmp_path, monkeypatch, "0016")
    data_db, events_db = _make_dbs(tmp_path)
    _run(tmp_path, data_db, events_db)
    with data_db.transaction() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, updated_ts)"
            " VALUES ('crosslist_markets', '[]', 100.0)"
        )
        _seed_read_thread(conn)

    _apply_0016(tmp_path, real_data, root, data_db, events_db)

    assert _connected(data_db) == ["carousell"]


@pytest.mark.parametrize(
    "market,source,why",
    [
        ("not-a-market", "browser_read", "an arbitrary identifier is not a marketplace"),
        ("carousell", "manual", "a thread we did not read off the market proves nothing"),
    ],
)
def test_0016_backfills_only_from_markets_we_demonstrably_read(
    tmp_path, monkeypatch, market, source, why
) -> None:
    """`create_thread` does not check its market against the registry, so the evidence is narrowed
    to a thread the read lane itself adopted."""
    real_data, root = _withhold(tmp_path, monkeypatch, "0016")
    data_db, events_db = _make_dbs(tmp_path)
    _run(tmp_path, data_db, events_db)
    with data_db.transaction() as conn:
        _seed_read_thread(conn, thread_id=f"{market}:1", market=market, source=source)

    _apply_0016(tmp_path, real_data, root, data_db, events_db)

    assert _connected(data_db) is None, why


def test_0015_starts_existing_rows_unchecked_and_unrestorable(tmp_path, monkeypatch) -> None:
    """verify_attempts 0 means "no lane has looked yet", and a NULL escalated_from_status means
    "do not know what to restore" — a guessed `active` would re-open a closed deal."""
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
