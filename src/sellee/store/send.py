"""The reserve/commit send bracket and the pacing gate in front of it."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from sellee.db import Database
from sellee.engines import pacing as pacing_engine
from sellee.store.helpers import ThreadNotFound, _new_id, _now


class SendMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

    if TYPE_CHECKING:
        # Owned by EscalationsMixin, called from the stale-intent sweep below. Declared and never
        # defined: only the composed Store has both mixins, and a checker looking at this class
        # alone cannot know that. The real body is the one that runs.
        def _open_escalation_in_txn(
            self, conn, thread_id: str, *, open_question: str, kind=None, context_summary=None
        ) -> tuple: ...

    # --- send bracket -----------------------------------------------------------------------

    def reserve_reply(
        self,
        *,
        thread_id: str,
        kind: str,
        text: str,
        in_msg_id: str | None,
        cfg,
        now: float | None = None,
        interactive: bool = False,
    ) -> dict:
        """Transaction A of the send bracket: pacing reserve + (only on `go`) a durable intent, in
        one transaction. A wait/quiet/unverified_open verdict records no pacing action and no
        intent — a blocked reply leaves nothing behind for a sweep to re-drive. Returns the verdict
        and, on go, the intent id; the caller performs the sink send outside this transaction."""
        now = now if now is not None else _now()
        with self._db.transaction() as conn:
            thread = conn.execute(
                "SELECT market FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if not thread:
                raise ThreadNotFound(f"no thread with id {thread_id!r}")
            # A send on this thread already ended unverified: the buyer may have that message, so
            # nothing further may be sent until it is settled. Refused here rather than left to the
            # caller, so no flow can talk past an unconfirmed send; the window is bounded — the
            # sweep folds the intent to unconfirmed and opens the escalation whose resolution is
            # the deliberate way back in.
            unverified = conn.execute(
                "SELECT 1 FROM send_intents WHERE thread_id = ? AND status = 'sent_unverified' "
                "LIMIT 1",
                (thread_id,),
            ).fetchone()
            if unverified:
                return {"verdict": "unverified_open", "delay_sec": 0.0}
            marketplace = thread["market"]
            cutoff = now - pacing_engine.WINDOW_SECONDS
            rows = conn.execute(
                "SELECT ts FROM pacing_actions WHERE marketplace = ? AND ts > ?",
                (marketplace, cutoff),
            ).fetchall()
            verdict = pacing_engine.evaluate(
                [r["ts"] for r in rows], now=now, cfg=cfg, kind=kind, interactive=interactive
            )
            if not verdict["record"]:
                return {"verdict": verdict["verdict"], "delay_sec": verdict["delay_sec"]}
            conn.execute(
                "INSERT INTO pacing_actions (marketplace, kind, ts) VALUES (?, ?, ?)",
                (marketplace, kind, now),
            )
            conn.execute(
                "DELETE FROM pacing_actions WHERE marketplace = ? AND ts <= ?",
                (marketplace, cutoff),
            )
            intent_id = _new_id("intent")
            conn.execute(
                "INSERT INTO send_intents "
                "(intent_id, thread_id, in_msg_id, text, kind, status, created_ts) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (intent_id, thread_id, in_msg_id, text, kind, now),
            )
            return {
                "verdict": "go",
                "delay_sec": verdict["delay_sec"],
                "intent_id": intent_id,
            }

    def commit_reply(
        self,
        *,
        intent_id: str,
        thread_id: str,
        in_msg_id: str | None,
        text: str,
        kind: str,
        pass_id: str | None = None,
        now: float | None = None,
    ) -> dict:
        """Transaction B: fold the outbound row (a deterministic msg_id from the intent id makes a
        retried commit a UNIQUE no-op), advance the cursor over the handled inbound, mark the intent
        committed, and stamp follow-up state — all in one transaction.

        The cursor lands on the newest message the *sender* was given, which for a pass is the
        watermark its claim recorded and never what is newest now. Stamping the current time instead
        would mark anything the buyer added mid-compose as handled by a reply that never saw it.
        """
        now = now if now is not None else _now()
        out_msg_id = f"out|{intent_id}"
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO thread_messages "
                "(thread_id, msg_id, dir, text, ts, source) VALUES (?, ?, 'out', ?, ?, 'agent')",
                (thread_id, out_msg_id, text, now),
            )
            conn.execute(
                "UPDATE send_intents SET status = 'committed', sent_ts = ?, committed_ts = ? "
                "WHERE intent_id = ?",
                (now, now, intent_id),
            )
            handled = self._cursor_target(conn, thread_id, in_msg_id, pass_id)
            if handled is not None:
                conn.execute(
                    "UPDATE threads SET cursor_last_msg_id = ?, cursor_last_ts = ?, "
                    "updated_ts = ? WHERE thread_id = ?",
                    (handled[0], handled[1], now, thread_id),
                )
            if kind == "followup":
                conn.execute(
                    "UPDATE threads SET last_followup_ts = ?, followup_disposition = 'sent', "
                    "updated_ts = ? WHERE thread_id = ?",
                    (now, now, thread_id),
                )
        return {"msg_id": out_msg_id}

    def _cursor_target(self, conn, thread_id: str, in_msg_id: str | None, pass_id: str | None):
        """How far this reply may advance the thread's cursor, as `(msg_id, ts)` or None.

        A pass's claim recorded the newest buyer message it was given, and that is the answer: it is
        a fact the daemon wrote, not something the sender reports, so a caller cannot advance past a
        message by claiming to have read it.

        A sender with no claim behind it — the seller's own session, or the channel pass carrying
        their words — names the message it answered, and falls back to the newest one on the thread.
        That fallback is not cosmetic: leaving the cursor where it was would keep the buyer waiting
        in the eligible set and earn them a second answer to the same question.
        """
        if pass_id is not None:
            claimed = self._claim_watermark(conn, pass_id, thread_id)
            if claimed is not None:
                return claimed
        if in_msg_id is not None:
            row = conn.execute(
                "SELECT ts FROM thread_messages WHERE thread_id = ? AND msg_id = ?",
                (thread_id, in_msg_id),
            ).fetchone()
            if row:
                return (in_msg_id, row["ts"])
        newest = conn.execute(
            "SELECT msg_id, ts FROM thread_messages WHERE thread_id = ? AND dir = 'in' "
            "ORDER BY ts DESC, rowid DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        return (newest["msg_id"], newest["ts"]) if newest else None

    def _claim_watermark(self, conn, pass_id: str, thread_id: str):
        row = conn.execute("SELECT payload FROM passes WHERE pass_id = ?", (pass_id,)).fetchone()
        if not row:
            return None
        claimed = (json.loads(row["payload"]) or {}).get("claimed_through") or {}
        found = claimed.get(thread_id)
        return (found[0], found[1]) if found else None

    def mark_intent_sent_unverified(self, intent_id: str) -> None:
        """Stamp an intent as clicked-but-not-yet-confirmed, between the send and its read-back.

        This is the state that distinguishes "we pressed send and do not know what happened" from
        "we never sent": the first must only ever be verified, never re-driven, or a buyer gets the
        same message twice. The sweep folds a stuck one to unconfirmed and escalates.
        """
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE send_intents SET status = 'sent_unverified', sent_ts = ? "
                "WHERE intent_id = ? AND status = 'pending'",
                (_now(), intent_id),
            )

    def intent_status(self, intent_id: str) -> str | None:
        """The send bracket's durable truth for one intent — what a caller consults after a sink
        failure, because the exception cannot say whether the page took the message."""
        rows = self._db.query("SELECT status FROM send_intents WHERE intent_id = ?", (intent_id,))
        return rows[0]["status"] if rows else None

    def record_manual_reply(self, thread_id: str, text: str, *, handle: str | None = None) -> dict:
        """Journal a reply the seller sent themselves in the marketplace app: an outbound row,
        deduped by normalized text, with NO cursor advance and no status change (the manual send
        means our account spoke last, so follow-ups stop treating the buyer as unanswered)."""
        normalized = " ".join((text or "").split()).lower()
        msg_id = "manual|" + hashlib.sha256(normalized.encode()).hexdigest()[:12]
        with self._db.transaction() as conn:
            if not conn.execute(
                "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone():
                raise ThreadNotFound(f"no thread with id {thread_id!r}")
            cur = conn.execute(
                "INSERT OR IGNORE INTO thread_messages "
                "(thread_id, msg_id, dir, text, ts, source) VALUES (?, ?, 'out', ?, ?, 'manual')",
                (thread_id, msg_id, text, _now()),
            )
            return {"recorded": cur.rowcount > 0, "deduped": cur.rowcount == 0}

    def stale_intent_sweep(self, grace_sec: float, now: float | None = None) -> list[dict]:
        """Fold intents stuck un-committed past the grace window as `unconfirmed` and open an
        escalation to verify whether the send fired — never a re-send. The deterministic msg_id
        means a genuinely-retried commit is still a no-op, so this only ever heals a real stall."""
        now = now if now is not None else _now()
        cutoff = now - grace_sec
        folded: list[dict] = []
        with self._db.transaction() as conn:
            stale = conn.execute(
                "SELECT intent_id, thread_id FROM send_intents "
                "WHERE status IN ('pending', 'sent_unverified') AND created_ts < ?",
                (cutoff,),
            ).fetchall()
            for row in stale:
                conn.execute(
                    "UPDATE send_intents SET status = 'unconfirmed' WHERE intent_id = ?",
                    (row["intent_id"],),
                )
                esc_id, new = self._open_escalation_in_txn(
                    conn,
                    row["thread_id"],
                    open_question="verify whether this reply was actually sent",
                    kind="unconfirmed_send",
                )
                folded.append(
                    {
                        "intent_id": row["intent_id"],
                        "thread_id": row["thread_id"],
                        "escalation_id": esc_id,
                        "escalation_new": new,
                    }
                )
        return folded

    # --- pacing -----------------------------------------------------------------------------

    def reserve_action(
        self,
        *,
        marketplace: str,
        kind: str,
        cfg,
        now: float | None = None,
        interactive: bool = False,
    ) -> dict:
        """Atomic check-and-record: count this marketplace's in-window actions, decide, and — only
        on `go` — insert the action and compact the ledger, all in one transaction (the
        serialization the legacy flock gave). On `go` the caller sleeps the returned jitter AFTER
        this returns; the DB lock is never held across that sleep."""
        now = now if now is not None else _now()
        cutoff = now - pacing_engine.WINDOW_SECONDS
        with self._db.transaction() as conn:
            rows = conn.execute(
                "SELECT ts FROM pacing_actions WHERE marketplace = ? AND ts > ?",
                (marketplace, cutoff),
            ).fetchall()
            timestamps = [r["ts"] for r in rows]
            result = pacing_engine.evaluate(
                timestamps, now=now, cfg=cfg, kind=kind, interactive=interactive
            )
            if result["record"]:
                conn.execute(
                    "INSERT INTO pacing_actions (marketplace, kind, ts) VALUES (?, ?, ?)",
                    (marketplace, kind, now),
                )
                conn.execute(
                    "DELETE FROM pacing_actions WHERE marketplace = ? AND ts <= ?",
                    (marketplace, cutoff),
                )
        result["marketplace"] = marketplace
        return result

    def peek_action(self, *, marketplace: str, kind: str, cfg, now: float | None = None) -> dict:
        """The verdict `reserve_action` would give right now, without taking the slot.

        For a caller that must decide before spending something a refusal would waste. `record` is
        forced False so this can never be mistaken for a reservation — a `go` here promises only
        that the cap and the window were open when it asked.
        """
        now = now if now is not None else _now()
        rows = self._db.query(
            "SELECT ts FROM pacing_actions WHERE marketplace = ? AND ts > ?",
            (marketplace, now - pacing_engine.WINDOW_SECONDS),
        )
        result = pacing_engine.evaluate([row["ts"] for row in rows], now=now, cfg=cfg, kind=kind)
        result["record"] = False
        result["marketplace"] = marketplace
        return result
