"""Forward-only numbered SQL migrations, applied at startup — one runner for both DBs.

Each DB (data = selly.db, events = events.db) has its own directory of NNNN_*.sql files and
its own schema_migrations table. A migration is applied in a single transaction (SQLite DDL
is transactional), so a failure mid-file rolls back and aborts startup rather than leaving a
half-migrated DB. selly.db is snapshotted before its migrations run — but only when something
is actually pending, so a routine restart writes no snapshot. events.db is disposable and is
never snapshotted.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from selly_agent.db import Database

log = logging.getLogger(__name__)

_MIGRATIONS_ROOT = Path(__file__).resolve().parent
_FILENAME_RE = re.compile(r"^(\d{4})_.*\.sql$")

DATA = "data"
EVENTS = "events"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path


@dataclass(frozen=True)
class Applied:
    db: str
    version: int
    name: str


def _discover(db_name: str) -> list[Migration]:
    directory = _MIGRATIONS_ROOT / db_name
    found: list[Migration] = []
    seen: dict[int, str] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if not match:
            raise ValueError(f"migration filename must be NNNN_*.sql: {path.name}")
        version = int(match.group(1))
        if version in seen:
            raise ValueError(
                f"duplicate migration version {version:04d}: {seen[version]} and {path.name}"
            )
        seen[version] = path.name
        found.append(Migration(version=version, name=path.name, path=path))
    return sorted(found, key=lambda m: m.version)


def _ensure_migrations_table(db: Database) -> None:
    db.run_atomic_script(
        "BEGIN IMMEDIATE;\n"
        "CREATE TABLE IF NOT EXISTS schema_migrations (\n"
        "  version    INTEGER PRIMARY KEY,\n"
        "  name       TEXT NOT NULL,\n"
        "  applied_ts REAL NOT NULL\n"
        ");\n"
        "COMMIT;\n"
    )


def _applied_versions(db: Database) -> set[int]:
    return {row["version"] for row in db.query("SELECT version FROM schema_migrations")}


def pending(db_name: str, db: Database) -> list[Migration]:
    _ensure_migrations_table(db)
    applied = _applied_versions(db)
    return [m for m in _discover(db_name) if m.version not in applied]


def _apply(db: Database, migration: Migration) -> None:
    body = migration.path.read_text().strip()
    name_literal = migration.name.replace("'", "''")
    # The record insert rides inside the same transaction as the DDL, so a version is never
    # marked applied unless its migration fully committed.
    script = (
        "BEGIN IMMEDIATE;\n"
        f"{body}\n"
        "INSERT INTO schema_migrations (version, name, applied_ts) "
        f"VALUES ({migration.version}, '{name_literal}', {time.time()!r});\n"
        "COMMIT;\n"
    )
    db.run_atomic_script(script)


def _snapshot(db_path: Path, pre_version: int, backups_dir: Path, backups_keep: int) -> Path:
    """Copy selly.db to a pre-migration backup via the sqlite3 backup API (never a file copy —
    a live WAL DB must not be byte-copied). Then prune to the newest `backups_keep`."""
    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    dest = backups_dir / f"selly-{ts}-pre-{pre_version:04d}.db"
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    prune_backups(backups_dir, backups_keep)
    return dest


def prune_backups(backups_dir: Path, backups_keep: int) -> None:
    """Keep the newest `backups_keep` selly.db snapshots; remove the rest. Shared by the
    snapshot step here and the retention task, so the retention policy has one source."""
    backups = sorted(
        backups_dir.glob("selly-*.db"),
        key=lambda p: (p.stat().st_mtime, p.name),
        reverse=True,
    )
    for stale in backups[backups_keep:]:
        try:
            stale.unlink()
        except OSError as exc:  # a backup we can't remove is not worth aborting startup
            log.warning("could not prune backup %s: %s", stale, exc)


def run_startup_migrations(
    *,
    data_db: Database,
    events_db: Database,
    backups_dir: Path,
    backups_keep: int,
) -> list[Applied]:
    """Snapshot-if-pending then apply pending migrations to both DBs. Returns what was applied
    (the daemon emits a migration.applied event per entry once the event bus is open)."""
    applied: list[Applied] = []

    data_pending = pending(DATA, data_db)
    if data_pending:
        _snapshot(data_db.path, data_pending[0].version, backups_dir, backups_keep)
    for migration in data_pending:
        _apply(data_db, migration)
        applied.append(Applied(db=DATA, version=migration.version, name=migration.name))

    for migration in pending(EVENTS, events_db):
        _apply(events_db, migration)
        applied.append(Applied(db=EVENTS, version=migration.version, name=migration.name))

    return applied
