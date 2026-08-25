"""The needs-a-human escalation ledger."""

from __future__ import annotations

from sellee.db import Database
from sellee.store.helpers import (
    EscalationRecord,
    StoreError,
    ThreadNotFound,
    _escalation_from_row,
    _new_id,
    _now,
)


class EscalationsMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

    # --- escalations ------------------------------------------------------------------------

    def escalate(
        self,
        thread_id: str,
        *,
        open_question: str,
        kind: str | None = None,
        context_summary: str | None = None,
    ) -> dict:
        """Open an escalation against a REAL thread (no synthetic ids — the 2026-06-29 incident):
        it records the open question, flips the thread to escalated, and is idempotent — a second
        escalate on a thread with an open escalation returns the existing id, changing nothing."""
        with self._db.transaction() as conn:
            if not conn.execute(
                "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone():
                raise ThreadNotFound(f"no thread with id {thread_id!r}")
            esc_id, new = self._open_escalation_in_txn(
                conn,
                thread_id,
                open_question=open_question,
                kind=kind,
                context_summary=context_summary,
            )
            return {"id": esc_id, "idempotent": not new}

    def _open_escalation_in_txn(
        self, conn, thread_id: str, *, open_question: str, kind=None, context_summary=None
    ) -> tuple:
        """Open an escalation for a thread within an existing transaction (shared by escalate and
        the stale-intent sweep). Idempotent: an existing open escalation is returned unchanged.
        Returns (escalation_id, is_new)."""
        thread = conn.execute(
            "SELECT side, item_id, want_id FROM threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        existing = conn.execute(
            "SELECT id FROM escalations WHERE thread_id = ? AND status = 'open'", (thread_id,)
        ).fetchone()
        if existing:
            return existing["id"], False
        esc_id = _new_id("esc")
        conn.execute(
            "INSERT INTO escalations "
            "(id, thread_id, side, item_id, want_id, kind, open_question, context_summary, "
            " status, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (
                esc_id,
                thread_id,
                thread["side"],
                thread["item_id"],
                thread["want_id"],
                kind,
                open_question,
                context_summary,
                _now(),
            ),
        )
        conn.execute(
            "UPDATE threads SET status = 'escalated', updated_ts = ? WHERE thread_id = ?",
            (_now(), thread_id),
        )
        return esc_id, True

    def resolve_escalation(self, escalation_id: str, resolution: str) -> dict:
        """Stamp an escalation resolved. Thread reactivation stays the caller's update_thread —
        this only closes the escalation record (the substrate any alarm path checks first)."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM escalations WHERE id = ?", (escalation_id,)
            ).fetchone()
            if not row:
                raise StoreError(f"no escalation with id {escalation_id!r}")
            conn.execute(
                "UPDATE escalations SET status = 'resolved', resolution = ?, resolved_ts = ? "
                "WHERE id = ?",
                (resolution, _now(), escalation_id),
            )
        return {"id": escalation_id, "status": "resolved"}

    def count_open_escalations(self) -> int:
        rows = self._db.query("SELECT COUNT(*) AS n FROM escalations WHERE status = 'open'")
        return rows[0]["n"]

    def open_escalation_thread_ids(self) -> set:
        """Threads with an open escalation — excluded from follow-up eligibility."""
        rows = self._db.query("SELECT DISTINCT thread_id FROM escalations WHERE status = 'open'")
        return {r["thread_id"] for r in rows}

    def list_open_escalations(self) -> list[EscalationRecord]:
        """Every open escalation, oldest first — the needs-me read the catchup surface renders.
        Escalations clear only via resolve_escalation; a read never stamps them."""
        rows = self._db.query(
            "SELECT * FROM escalations WHERE status = 'open' ORDER BY created_ts ASC, id ASC"
        )
        return [_escalation_from_row(r) for r in rows]

    def get_escalation(self, escalation_id: str) -> EscalationRecord | None:
        rows = self._db.query("SELECT * FROM escalations WHERE id = ?", (escalation_id,))
        return _escalation_from_row(rows[0]) if rows else None
