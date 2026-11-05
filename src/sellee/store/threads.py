"""Threads and their messages."""

from __future__ import annotations

import json

from sellee.db import Database
from sellee.store.helpers import (
    _THREAD_SIDES,
    _TRANSCRIPT_DEFAULT_CAP,
    SCAM_CONFIRMATION,
    ItemNotFound,
    MessageRecord,
    StoreError,
    ThreadNotFound,
    ThreadRecord,
    ThreadSummary,
    WantNotFound,
    _now,
    _thread_from_row,
)


class ThreadsMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

    # --- threads ----------------------------------------------------------------------------

    def create_thread(
        self,
        *,
        thread_id: str,
        side: str,
        market: str,
        counterpart_handle: str,
        item_id: str | None = None,
        want_id: str | None = None,
        listing_url: str | None = None,
        listed_price: float | None = None,
        buyer_location: str | None = None,
        source: str | None = None,
    ) -> ThreadRecord:
        """Create an identity-complete thread. The natural key is the caller-supplied
        `<market>:<local id>`; identity (side, market, counterpart, and the owning item/want) is
        required at creation so the suppression layers that key off it can never be disabled by a
        skeleton thread."""
        if side not in _THREAD_SIDES:
            raise StoreError(f"side must be one of {_THREAD_SIDES}, got {side!r}")
        if not market or not market.strip():
            raise StoreError("market must be non-empty")
        if not thread_id or not thread_id.startswith(f"{market}:"):
            raise StoreError(f"thread_id must start with {market!r} + ':' (got {thread_id!r})")
        if not counterpart_handle or not counterpart_handle.strip():
            raise StoreError("counterpart_handle must be non-empty")
        if side == "sell" and not item_id:
            raise StoreError("a sell thread requires item_id")
        if side == "buy" and not want_id:
            raise StoreError("a buy thread requires want_id")
        ts = _now()
        with self._db.transaction() as conn:
            if conn.execute("SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)).fetchone():
                raise StoreError(f"thread {thread_id!r} already exists")
            if (
                item_id
                and not conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone()
            ):
                raise ItemNotFound(f"no item with id {item_id!r}")
            if (
                want_id
                and not conn.execute("SELECT 1 FROM wants WHERE want_id = ?", (want_id,)).fetchone()
            ):
                raise WantNotFound(f"no want with id {want_id!r}")
            conn.execute(
                "INSERT INTO threads (thread_id, side, market, item_id, want_id, "
                "counterpart_handle, status, listing_url, listed_price, buyer_location, source, "
                "created_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    side,
                    market.strip(),
                    item_id,
                    want_id,
                    counterpart_handle.strip(),
                    listing_url,
                    listed_price,
                    buyer_location,
                    source,
                    ts,
                    ts,
                ),
            )
        return self.get_thread(thread_id)  # type: ignore[return-value]

    def get_thread(
        self, thread_id: str, *, message_cap: int = _TRANSCRIPT_DEFAULT_CAP
    ) -> ThreadRecord | None:
        rows = self._db.query("SELECT * FROM threads WHERE thread_id = ?", (thread_id,))
        if not rows:
            return None
        message_count = self._db.query(
            "SELECT COUNT(*) AS n FROM thread_messages WHERE thread_id = ?", (thread_id,)
        )[0]["n"]
        record: ThreadRecord = {
            **_thread_from_row(rows[0]),
            "messages": self.get_thread_messages(thread_id, limit=message_cap),
            "message_count": message_count,
            "scam_cleared": bool(
                self._db.query(
                    "SELECT 1 FROM escalations WHERE thread_id = ? AND status = 'resolved' "
                    "AND resolution = 'not scam' LIMIT 1",
                    (thread_id,),
                )
            ),
        }
        return record

    def list_threads(
        self, side: str | None = None, status: str | None = None
    ) -> list[ThreadSummary]:
        clauses, params = [], []
        if side is not None:
            clauses.append("side = ?")
            params.append(side)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM threads"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_ts DESC"
        rows = self._db.query(sql, tuple(params))
        return [_thread_from_row(r) for r in rows]

    def append_thread_message(
        self,
        thread_id: str,
        *,
        msg_id: str,
        direction: str,
        text: str,
        ts: float | None = None,
        source: str | None = None,
        scam_verdict: str | None = None,
        carousell_ai_risk: str | None = None,
        carousell_ai_flags: list | None = None,
    ) -> bool:
        """Fold one transcript row into a thread, deduped by (thread_id, msg_id). Returns True if
        it was newly inserted, False if the constraint dropped a duplicate — dedup by the schema,
        never by a read-then-write race."""
        if direction not in ("in", "out"):
            raise StoreError("direction must be 'in' or 'out'")
        with self._db.transaction() as conn:
            if not conn.execute(
                "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone():
                raise ThreadNotFound(f"no thread with id {thread_id!r}")
            cur = conn.execute(
                "INSERT OR IGNORE INTO thread_messages "
                "(thread_id, msg_id, dir, text, ts, source, scam_verdict, carousell_ai_risk, "
                " carousell_ai_flags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    msg_id,
                    direction,
                    text,
                    ts if ts is not None else _now(),
                    source,
                    scam_verdict,
                    carousell_ai_risk,
                    json.dumps(carousell_ai_flags) if carousell_ai_flags else None,
                ),
            )
            return cur.rowcount > 0

    def record_inbound(
        self,
        thread_id: str,
        *,
        msg_id: str,
        text: str,
        ts: float | None = None,
        direction: str = "in",
        scam_verdict: str | None = None,
    ) -> bool:
        """Record one message the scripted marketplace read saw, idempotent on msg_id.

        Deliberately does NOT advance the reply cursor — only a committed reply does. An outbound
        bubble we did not write is recorded too: that is the seller replying by hand in their app,
        and recording it is what stops a double-message.
        """
        return self.append_thread_message(
            thread_id,
            msg_id=msg_id,
            direction=direction,
            text=text,
            ts=ts,
            source="marketplace" if direction == "in" else "manual",
            scam_verdict=scam_verdict,
        )

    def get_thread_messages(
        self, thread_id: str, *, limit: int | None = None
    ) -> list[MessageRecord]:
        """The transcript in chronological order, capped to the most recent `limit` rows."""
        columns = (
            "msg_id, dir, text, ts, source, scam_verdict, carousell_ai_risk, carousell_ai_flags"
        )
        if limit is None:
            rows = self._db.query(
                f"SELECT {columns} FROM thread_messages "
                "WHERE thread_id = ? ORDER BY ts ASC, rowid ASC",
                (thread_id,),
            )
        else:
            rows = self._db.query(
                f"SELECT {columns} FROM thread_messages "
                "WHERE thread_id = ? ORDER BY ts DESC, rowid DESC LIMIT ?",
                (thread_id, limit),
            )
            rows = list(reversed(rows))
        return [
            {
                "msg_id": r["msg_id"],
                "dir": r["dir"],
                "text": r["text"],
                "ts": r["ts"],
                "source": r["source"],
                "scam_verdict": r["scam_verdict"],
                "carousell_ai_risk": r["carousell_ai_risk"],
                "carousell_ai_flags": json.loads(r["carousell_ai_flags"] or "[]"),
            }
            for r in rows
        ]

    def newest_inbound_msg_id(self, thread_id: str) -> str | None:
        """The id of the last message the counterpart wrote on this thread, or None."""
        rows = self._db.query(
            "SELECT msg_id FROM thread_messages WHERE thread_id = ? AND dir = 'in' "
            "ORDER BY ts DESC, rowid DESC LIMIT 1",
            (thread_id,),
        )
        return rows[0]["msg_id"] if rows else None

    _THREAD_WRITABLE = ("buyer_location", "agent_note", "listed_price", "listing_url")
    # The only status flips this generic writer owns. held is owned by hold/release, escalated by
    # escalate, and the sale states by the confirm-sold / buyer-accept flows.
    _THREAD_STATUS_TRANSITIONS = frozenset({("escalated", "active"), ("active", "closed")})

    def update_thread(self, thread_id: str, fields: dict) -> ThreadRecord:
        status = fields.get("status")
        other = {k: v for k, v in fields.items() if k != "status"}
        unknown = [k for k in other if k not in self._THREAD_WRITABLE]
        if unknown:
            raise StoreError(
                f"unknown or non-writable thread field(s): {', '.join(sorted(unknown))}; "
                f"writable: {', '.join(self._THREAD_WRITABLE)} (transcript/cursor advance via "
                "send_reply/record_manual_reply/mark_relay_answered; held via hold_thread; "
                "sale states via the confirm flows)"
            )
        if not fields:
            raise StoreError("no fields to update")
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if not row:
                raise ThreadNotFound(f"no thread with id {thread_id!r}")
            assignments = dict(other)
            if status is not None:
                if (row["status"], status) not in self._THREAD_STATUS_TRANSITIONS:
                    raise StoreError(
                        f"status {row['status']!r}->{status!r} is not allowed here; held is owned "
                        "by hold_thread/release_thread, escalated by escalate, and sale states by "
                        "negotiate_confirm_sold / buyer_negotiate_accept"
                    )
                # Reactivating is only legitimate once the question has actually been answered. The
                # escalation, not the status, is the substrate: leaving it open and flipping the
                # status back is how a refused send gets sent anyway — on 2026-08-27 a pass hit
                # "thread is 'escalated' — not eligible for a reply" and, twice, called this with
                # status=active to get past it. resolve_escalation first; it is one extra call and
                # it records WHY the thread may talk again.
                if (
                    row["status"] == "escalated"
                    and conn.execute(
                        "SELECT 1 FROM escalations WHERE thread_id = ? AND status = 'open' LIMIT 1",
                        (thread_id,),
                    ).fetchone()
                ):
                    raise StoreError(
                        f"thread {thread_id!r} still has an open escalation — resolve_escalation "
                        "first; reactivating around an unanswered question is not allowed"
                    )
                assignments["status"] = status
                if status == "closed":
                    assignments["closed_ts"] = _now()
                if row["status"] == "escalated":
                    assignments["escalated_from_status"] = None
            if not assignments:
                raise StoreError("no fields to update")
            clause = ", ".join(f"{name} = ?" for name in assignments)
            conn.execute(
                f"UPDATE threads SET {clause}, updated_ts = ? WHERE thread_id = ?",
                (*assignments.values(), _now(), thread_id),
            )
        return self.get_thread(thread_id)  # type: ignore[return-value]

    def hold_thread(
        self, thread_id: str, reason: str, mark_handled_msg: str | None = None
    ) -> ThreadRecord:
        """Flip a thread to held, preserving the pre-hold status so release can restore it. A
        re-hold keeps the original held_from_status; mark_handled advances the cursor (the hold IS
        the handling — no reply is sent)."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT status, held_from_status FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if not row:
                raise ThreadNotFound(f"no thread with id {thread_id!r}")
            if row["status"] == "held":
                held_from = row["held_from_status"] or "active"
            else:
                held_from = row["status"] or "active"
            if mark_handled_msg:
                conn.execute(
                    "UPDATE threads SET status = 'held', held_reason = ?, held_from_status = ?, "
                    "cursor_last_msg_id = ?, cursor_last_ts = ?, updated_ts = ? "
                    "WHERE thread_id = ?",
                    (reason, held_from, mark_handled_msg, _now(), _now(), thread_id),
                )
            else:
                conn.execute(
                    "UPDATE threads SET status = 'held', held_reason = ?, held_from_status = ?, "
                    "updated_ts = ? WHERE thread_id = ?",
                    (reason, held_from, _now(), thread_id),
                )
        return self.get_thread(thread_id)  # type: ignore[return-value]

    def release_thread(self, thread_id: str) -> ThreadRecord:
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT status, held_from_status FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if not row:
                raise ThreadNotFound(f"no thread with id {thread_id!r}")
            restored = row["held_from_status"] or "active"
            conn.execute(
                "UPDATE threads SET status = ?, held_reason = NULL, held_from_status = NULL, "
                "updated_ts = ? WHERE thread_id = ?",
                (restored, _now(), thread_id),
            )
        return self.get_thread(thread_id)  # type: ignore[return-value]

    # The open ask a seller's scam answer settles: one opened as the scam confirmation, or one
    # opened before that kind existed on a thread that still holds a scam.
    _OPEN_SCAM_CONFIRMATION_SQL = (
        "SELECT id FROM escalations WHERE thread_id = ? AND status = 'open' AND (kind = ? OR "
        "EXISTS (SELECT 1 FROM thread_messages WHERE thread_id = escalations.thread_id "
        "AND scam_verdict = 'scam'))"
    )

    def open_scam_confirmation(self, thread_id: str) -> str | None:
        """The id of the thread's open scam confirmation, or None."""
        rows = self._db.query(self._OPEN_SCAM_CONFIRMATION_SQL, (thread_id, SCAM_CONFIRMATION))
        return rows[0]["id"] if rows else None

    def settle_scam_confirmation(self, thread_id: str, answer: str) -> dict:
        """`not_scam` lowers each `scam` verdict to `suspicious` and restores the status from
        before hold and ask; `keep_held` leaves the verdicts and holds the thread."""
        if answer not in ("not_scam", "keep_held"):
            raise StoreError(f"unknown scam confirmation answer {answer!r}")
        ts = _now()
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT status, held_from_status, escalated_from_status FROM threads "
                "WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if not row:
                raise ThreadNotFound(f"no thread with id {thread_id!r}")
            asked = conn.execute(
                self._OPEN_SCAM_CONFIRMATION_SQL, (thread_id, SCAM_CONFIRMATION)
            ).fetchone()
            if asked is None:
                raise StoreError(f"thread {thread_id!r} has no scam confirmation open")
            conn.execute(
                "UPDATE escalations SET status = 'resolved', resolution = ?, resolved_ts = ? "
                "WHERE id = ?",
                (answer.replace("_", " "), ts, asked["id"]),
            )
            status, seen = row["status"], set()
            # A repeated hold over an ask can point the two back at each other, so stop on a loop.
            while status in ("held", "escalated") and status not in seen:
                seen.add(status)
                status = row[f"{status}_from_status"] or "active"
            if status in ("held", "escalated"):
                status = "active"
            lowered = 0
            if answer == "keep_held":
                conn.execute(
                    "UPDATE threads SET status = 'held', held_from_status = ?, "
                    "held_reason = COALESCE(held_reason, 'scam'), escalated_from_status = NULL, "
                    "updated_ts = ? WHERE thread_id = ?",
                    (status, ts, thread_id),
                )
                status = "held"
            else:
                lowered = conn.execute(
                    "UPDATE thread_messages SET scam_verdict = 'suspicious' "
                    "WHERE thread_id = ? AND scam_verdict = 'scam'",
                    (thread_id,),
                ).rowcount
                conn.execute(
                    "UPDATE threads SET status = ?, held_reason = NULL, held_from_status = NULL, "
                    "escalated_from_status = NULL, updated_ts = ? WHERE thread_id = ?",
                    (status, ts, thread_id),
                )
        return {
            "thread_id": thread_id,
            "status": status,
            "escalation_id": asked["id"],
            "lowered": lowered,
        }
