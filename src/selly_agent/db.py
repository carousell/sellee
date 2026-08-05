"""SQLite access — WAL, one write connection per DB behind a lock, read-only readers.

State is stdlib sqlite3 and no ORM: the schema is small and the migrations are explicit SQL. WAL
gives concurrent readers (the logs tail / web tail) by construction. The write connection runs in
autocommit mode (isolation_level=None) with explicit BEGIN IMMEDIATE / COMMIT, because implicit
transactions muddy DDL. All writes go through the one connection serialized by a lock; readers
open independent read-only URI connections.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_BUSY_TIMEOUT_MS = 5000


def _apply_pragmas(conn: sqlite3.Connection, *, writable: bool) -> None:
    if writable:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")


def connect_writer(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn, writable=True)
    return conn


def read_only_uri(path: Path) -> str:
    """The `file:` URI addressing `path` read-only.

    Built rather than interpolated: `file:C:\\Users\\...` is not a URI — SQLite reads the drive
    letter as a scheme and the backslashes as literal characters, and opens the wrong database or
    none. as_uri() gives the `file:///C:/...` form, and escapes what needs escaping.
    """
    return f"{Path(path).absolute().as_uri()}?mode=ro"


def connect_reader(path: Path) -> sqlite3.Connection:
    """A read-only connection. Works whether or not a writer is live — even against a stopped
    daemon's file (the `logs` tail of history)."""
    uri = read_only_uri(path)
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn, writable=False)
    return conn


class Database:
    """A single write connection to one DB file, serialized behind a lock."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._conn = connect_writer(self.path)
        self._lock = threading.Lock()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A real transaction: BEGIN IMMEDIATE on entry, COMMIT on success, ROLLBACK on error."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def run_atomic_script(self, script: str) -> None:
        """Execute a multi-statement script that carries its own BEGIN/COMMIT, atomically.

        executescript ignores isolation_level and always issues a leading COMMIT, so the
        transaction control must live inside the script (a migration does exactly this). On
        failure the connection is left mid-transaction; we roll it back before re-raising.
        """
        with self._lock:
            try:
                self._conn.executescript(script)
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
