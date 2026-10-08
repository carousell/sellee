"""The relay and registration lanes' state: their cursors, the relay threads read again, the
registration mail handled, and the reply cursor the relay moves."""

from __future__ import annotations

from sellee.db import Database
from sellee.store.helpers import _REPLY_THREAD_STATUSES, ThreadNotFound, _now


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

    def relay_rereads(self) -> list[dict]:
        """The relay threads the lane reads again this tick, oldest first."""
        rows = self._db.query(
            "SELECT bazaar_thread_id, listing_id, placed FROM relay_rereads ORDER BY added_ts"
        )
        return [
            {
                "id": r["bazaar_thread_id"],
                "listing_id": r["listing_id"],
                "placed": bool(r["placed"]),
            }
            for r in rows
        ]

    def keep_relay_reread(self, bazaar_thread_id: str, listing_id: str, *, placed: bool) -> None:
        """Remember a thread to read again. `placed` is False while no item has its listing."""
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO relay_rereads (bazaar_thread_id, listing_id, placed, added_ts) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (bazaar_thread_id) DO UPDATE SET "
                "listing_id = excluded.listing_id, placed = excluded.placed",
                (bazaar_thread_id, listing_id, int(placed), _now()),
            )

    def drop_relay_reread(self, bazaar_thread_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM relay_rereads WHERE bazaar_thread_id = ?", (bazaar_thread_id,)
            )

    def close_blocked_relay_thread(self, thread_id: str) -> bool:
        """Close a thread whose buyer bazaar blocked, from any status still open to replies.
        False while it is held or escalated, so the close waits for its release."""
        ts = _now()
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if not row:
                raise ThreadNotFound(f"no thread with id {thread_id!r}")
            if row["status"] in ("held", "escalated"):
                return False
            if row["status"] in _REPLY_THREAD_STATUSES:
                conn.execute(
                    "UPDATE threads SET status = 'closed', closed_ts = ?, updated_ts = ? "
                    "WHERE thread_id = ?",
                    (ts, ts, thread_id),
                )
            return True

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

    def get_registration_cursor(self) -> str:
        """Where the next list_registration_mail poll resumes; empty before the first poll."""
        rows = self._db.query("SELECT cursor FROM registration_cursor WHERE id = 1")
        return rows[0]["cursor"] if rows else ""

    def set_registration_cursor(self, cursor: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO registration_cursor (id, cursor, updated_ts) VALUES (1, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET cursor = excluded.cursor, "
                "updated_ts = excluded.updated_ts",
                (cursor, _now()),
            )

    def registration_mail_seen(self, mail_id: str) -> bool:
        rows = self._db.query("SELECT 1 FROM registration_seen WHERE mail_id = ?", (mail_id,))
        return bool(rows)

    def mark_registration_mail_seen(
        self, mail_id: str, *, thread_id: str | None, subject: str, received_ts: float
    ) -> None:
        """Record a mail as handled; `thread_id` is the thread it joined, or None if dropped."""
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO registration_seen "
                "(mail_id, thread_id, subject, received_ts, seen_ts) VALUES (?, ?, ?, ?, ?)",
                (mail_id, thread_id, subject, received_ts, _now()),
            )

    def latest_registration_subject(self, thread_id: str) -> str:
        """The subject of the newest mail that joined this thread, or "" when none did."""
        rows = self._db.query(
            "SELECT subject FROM registration_seen WHERE thread_id = ? "
            "ORDER BY received_ts DESC, mail_id DESC LIMIT 1",
            (thread_id,),
        )
        return rows[0]["subject"] if rows else ""
