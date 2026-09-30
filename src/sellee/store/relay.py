"""The relay lane's state: its list_threads cursor, and the reply cursor a relayed answer moves."""

from __future__ import annotations

from sellee.db import Database
from sellee.store.helpers import ThreadNotFound, _now


class RelayMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

    def get_relay_cursor(self) -> str:
        """Where the next list_threads poll resumes; empty before the first poll."""
        rows = self._db.query("SELECT cursor FROM relay_cursor WHERE id = 1")
        return rows[0]["cursor"] if rows else ""

    def set_relay_cursor(self, cursor: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO relay_cursor (id, cursor, updated_ts) VALUES (1, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET cursor = excluded.cursor, "
                "updated_ts = excluded.updated_ts",
                (cursor, _now()),
            )

    def mark_relay_answered(self, thread_id: str, msg_id: str, ts: float) -> None:
        """Move the reply cursor to a buyer message bazaar shows answered, and never back."""
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE threads SET cursor_last_msg_id = ?, cursor_last_ts = ?, updated_ts = ? "
                "WHERE thread_id = ? AND (cursor_last_ts IS NULL OR cursor_last_ts < ?)",
                (msg_id, ts, _now(), thread_id, ts),
            )
            if (
                cur.rowcount == 0
                and not conn.execute(
                    "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)
                ).fetchone()
            ):
                raise ThreadNotFound(f"no thread with id {thread_id!r}")
