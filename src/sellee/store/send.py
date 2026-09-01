"""The reserve/commit send bracket and the pacing gate in front of it."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from sellee.db import Database
from sellee.engines import pacing as pacing_engine
from sellee.store.helpers import ThreadNotFound, _new_id, _now

# How hard the machine tries before the seller is asked anything. An unsettled send is re-read by
# the inbox lane on its own cadence; two misses at that cadence is roughly the grace window the
# sweep already waited, so a genuinely unconfirmable send still reaches the seller on the old timing
# — what changed is that a send nobody has looked at yet no longer does.
MIN_VERIFY_ATTEMPTS = 2
# The backstop for the failure no waiting fixes: a lane that cannot run at all leaves the attempt
# count at zero forever, and an unconfirmed message to a real buyer cannot just sit there.
HARD_GRACE_SEC = 3600.0
# And when to stop looking. A message that is really not on the page — the seller deleted it, or
# the marketplace removed it — will never be found, and without a ceiling the lane would force-open
# that conversation on every tick for the life of the install. Generous enough to cover a slow sync
# at the inbox lane's cadence, then the intent is left as the seller's answer settled it.
MAX_VERIFY_ATTEMPTS = 20

# The one place this ask is worded. It is authored here rather than by a model because it may only
# ever be raised by a send that survived the whole gate above — a pass that writes it itself is
# asking the seller to do the machine's job, which is exactly what happened on 2026-08-27.
UNCONFIRMED_SEND_ASK = (
    "I sent a reply to this buyer but still can't confirm it arrived, even after re-checking the "
    "chat. Could you open the conversation in your app — is my message there?"
)
UNCONFIRMED_SEND_CONTEXT = (
    "A send was accepted by the marketplace page but never read back, and re-reading the "
    "conversation has not found it either. Nothing further goes to this buyer until this is "
    "settled, and the message is never re-sent without the seller's answer."
)
# The same ask, worded for the case where the machine never actually looked. Only the hard-grace
# backstop can reach it, and claiming "even after re-checking the chat" there is a lie about work
# nobody did — which is the whole of what makes the ask offensive to receive. Same buttons and same
# kind, so an answer settles it identically and a later read still withdraws it.
UNCHECKED_SEND_ASK = (
    "I sent a reply to this buyer and haven't been able to open the conversation since, so I still "
    "can't tell you whether it arrived — that's me not having looked, not the message being "
    "missing. Could you open it in your app — is my message there?"
)
UNCHECKED_SEND_CONTEXT = (
    "A send was accepted by the marketplace page but never read back, and every attempt to re-open "
    "the conversation since has failed, so it has never been checked. Nothing further goes to this "
    "buyer until this is settled, and the message is never re-sent without the seller's answer."
)
UNCONFIRMED_SEND_OPTIONS = ("✅ It's there", "🚫 Nothing there")

# Every status meaning "we still do not know whether the buyer got this". `pending` never got past
# the composer, `sent_unverified` was taken by the page and could not be read back, and
# `unconfirmed` is one the sweep gave up on and asked the seller about. All three are answered by
# looking at the conversation, so the lane keeps looking at all three — finding the bubble after the
# ask went out is what lets the ask be withdrawn instead of the seller having to answer it.
UNSETTLED_STATUSES = ("pending", "sent_unverified", "unconfirmed")
_UNSETTLED_PLACEHOLDERS = ", ".join("?" for _ in UNSETTLED_STATUSES)


class SendMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

    if TYPE_CHECKING:
        # Owned by EscalationsMixin, called from the sweep and the settle path below. Declared and
        # never defined: only the composed Store has both mixins, and a checker looking at this
        # class alone cannot know that. The real bodies are the ones that run.
        def _open_escalation_in_txn(
            self,
            conn,
            thread_id: str,
            *,
            open_question: str,
            kind=None,
            context_summary=None,
            options=None,
        ) -> tuple: ...

        def _resolve_escalations_in_txn(
            self, conn, thread_id: str, *, kind: str, resolution: str
        ) -> list: ...

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
        with self._db.transaction() as conn:
            return self._commit_reply_in_txn(
                conn,
                intent_id=intent_id,
                thread_id=thread_id,
                in_msg_id=in_msg_id,
                text=text,
                kind=kind,
                pass_id=pass_id,
                now=now,
            )

    def _commit_reply_in_txn(
        self, conn, *, intent_id, thread_id, in_msg_id, text, kind, pass_id, now
    ) -> dict:
        """Transaction B's body, callable from inside a larger transaction — shared with the settle
        path, so "a reply is committed" has exactly one definition wherever the confirmation came
        from (the send's own read-back, or a later lane finding the bubble on the page)."""
        out_msg_id = f"out|{intent_id}"
        conn.execute(
            "INSERT OR IGNORE INTO thread_messages "
            "(thread_id, msg_id, dir, text, ts, source) VALUES (?, ?, 'out', ?, ?, 'agent')",
            (thread_id, out_msg_id, text, now),
        )
        conn.execute(
            "UPDATE send_intents SET status = 'committed', sent_ts = COALESCE(sent_ts, ?), "
            "committed_ts = ? WHERE intent_id = ?",
            (now, now, intent_id),
        )
        handled = self._cursor_target(conn, thread_id, in_msg_id, pass_id, kind)
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

    def _cursor_target(
        self, conn, thread_id: str, in_msg_id: str | None, pass_id: str | None, kind: str = "reply"
    ):
        """How far this reply may advance the thread's cursor, as `(msg_id, ts)` or None.

        A pass's claim recorded the newest buyer message it was given, and that is the answer: it is
        a fact the daemon wrote, not something the sender reports, so a caller cannot advance past a
        message by claiming to have read it.

        A sender with no claim behind it — the seller's own session, or the channel pass carrying
        their words — names the message it answered, and falls back to the newest one on the thread.
        That fallback is not cosmetic: leaving the cursor where it was would keep the buyer waiting
        in the eligible set and earn them a second answer to the same question.

        A `holding` line advances NOTHING, because it answered nothing: it exists to keep the buyer
        warm while the seller is asked, and the question it was sent about is still open. Marking it
        handled is how a buyer gets stranded — on 2026-08-27 three of them were, permanently: their
        holding line moved the cursor past their offer, the seller's real answer was then dropped by
        the pacing cap, and `_UNHANDLED_INBOUND_SQL` could never see them again. The escalation that
        always accompanies a holding line is what keeps the thread out of the reply lane until the
        seller has actually answered.
        """
        if kind == "holding":
            return None
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

    def unsettled_intents(self, max_attempts: int = MAX_VERIFY_ATTEMPTS) -> list[dict]:
        """Every send whose fate is still unknown and still worth looking for.

        `pending` and `sent_unverified` are the two shapes of "we do not know": the first never
        got past the composer, the second was taken by the page and could not be read back.
        `unconfirmed` is a third — one the sweep gave up on and asked the seller about — and it is
        included on purpose, because finding the message after the ask went out is what lets the ask
        be withdrawn instead of the seller having to answer it.

        Capped by `verify_attempts`, because a message that is genuinely not on the page is never
        going to be: the seller deleted it, or the marketplace removed it. Without the cap the lane
        would force-open that one conversation on every tick forever — paying a navigate and a read
        each time to re-learn the same answer.
        """
        rows = self._db.query(
            "SELECT intent_id, thread_id, text, in_msg_id, kind, status, verify_attempts, "
            f"created_ts FROM send_intents WHERE status IN ({_UNSETTLED_PLACEHOLDERS}) "
            "AND verify_attempts < ? ORDER BY created_ts ASC",
            (*UNSETTLED_STATUSES, max_attempts),
        )
        return [dict(row) for row in rows]

    def bump_verify_attempt(self, intent_id: str) -> int:
        """Record that a lane looked for this message and did not find it. Returns the new count.

        This is what makes asking the seller a last resort rather than a timeout: the sweep can tell
        "nobody has checked yet" from "we have checked and it really is not there".
        """
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE send_intents SET verify_attempts = verify_attempts + 1 "
                f"WHERE intent_id = ? AND status IN ({_UNSETTLED_PLACEHOLDERS})",
                (intent_id, *UNSETTLED_STATUSES),
            )
            row = conn.execute(
                "SELECT verify_attempts FROM send_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return row["verify_attempts"] if row else 0

    def settle_intent_from_read(self, intent_id: str, now: float | None = None) -> dict | None:
        """Commit an unsettled intent because its message was just found on the page.

        The whole point of the self-settling loop: the reply is folded exactly as a verified send
        would have been (same deterministic `out|{intent_id}` msg_id, so a later commit is a UNIQUE
        no-op), any `unconfirmed_send` escalation it caused is withdrawn, and the thread goes back
        to the status it held before. One transaction, because a half-settled intent would either
        strand the thread as escalated or re-open a send path on a message we have not folded.

        Returns None when the intent is already settled — the lane re-reads freely, and a second
        find must change nothing. An `unconfirmed` intent settles too, and is the case that matters
        most: the seller has already been asked, so finding the message is what takes the question
        back off them.
        """
        now = now if now is not None else _now()
        with self._db.transaction() as conn:
            intent = conn.execute(
                "SELECT * FROM send_intents WHERE intent_id = ? "
                f"AND status IN ({_UNSETTLED_PLACEHOLDERS})",
                (intent_id, *UNSETTLED_STATUSES),
            ).fetchone()
            if intent is None:
                return None
            commit = self._commit_reply_in_txn(
                conn,
                intent_id=intent_id,
                thread_id=intent["thread_id"],
                in_msg_id=intent["in_msg_id"],
                text=intent["text"],
                kind=intent["kind"],
                # No claim stands behind a settle: the pass that reserved this intent is long gone,
                # so the cursor falls back to the message the intent itself named.
                pass_id=None,
                now=now,
            )
            resolved = self._resolve_escalations_in_txn(
                conn,
                intent["thread_id"],
                kind="unconfirmed_send",
                resolution="Found our own message on the page — the send did land.",
            )
        return {
            "intent_id": intent_id,
            "thread_id": intent["thread_id"],
            "msg_id": commit["msg_id"],
            "escalations_resolved": resolved,
        }

    def has_intent_for_threads(self, thread_ids, since_ts: float) -> bool:
        """Whether any of these threads gained a send intent at or after `since_ts`.

        "Did that pass actually try to send?" — the reply pass's progress test. Intent creation is
        the first durable mark the send bracket makes, and `reserve_reply` writes one only on a `go`
        verdict, so its absence across every claimed thread means the pass reached no send path at
        all. Every status counts: a `pending` or `sent_unverified` intent did reach the page, and
        the stale-intent sweep owns its fate from there.
        """
        ids = tuple(thread_ids)
        if not ids:
            return False
        placeholders = ",".join("?" for _ in ids)
        rows = self._db.query(
            f"SELECT 1 FROM send_intents WHERE thread_id IN ({placeholders}) "
            "AND created_ts >= ? LIMIT 1",
            (*ids, since_ts),
        )
        return bool(rows)

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

    def stale_intent_sweep(
        self,
        grace_sec: float,
        now: float | None = None,
        min_verify_attempts: int = MIN_VERIFY_ATTEMPTS,
        hard_grace_sec: float = HARD_GRACE_SEC,
    ) -> list[dict]:
        """Fold intents the machine could not settle as `unconfirmed` and ask the seller to look —
        never a re-send. The deterministic msg_id means a genuinely-retried commit is still a no-op,
        so this only ever heals a real stall.

        Gated on effort, not just on the clock. The lane re-reads each unsettled thread and counts
        its attempts, so an intent is only handed to the seller once we have actually looked for it
        `min_verify_attempts` times — otherwise the ask arrives before the machine has tried, which
        is the whole complaint this exists to answer. `hard_grace_sec` is the backstop for the case
        no amount of waiting fixes: a lane that cannot run at all, where the attempt count would
        stay at zero forever.

        Which of the two it was decides the wording. An ask that says "even after re-checking the
        chat" when nothing checked it is a claim about work nobody did, and it is what the seller
        reads as the machine handing them its own job.
        """
        now = now if now is not None else _now()
        cutoff = now - grace_sec
        hard_cutoff = now - hard_grace_sec
        folded: list[dict] = []
        with self._db.transaction() as conn:
            stale = conn.execute(
                "SELECT intent_id, thread_id, verify_attempts FROM send_intents "
                "WHERE status IN ('pending', 'sent_unverified') AND created_ts < ? "
                "AND (verify_attempts >= ? OR created_ts < ?)",
                (cutoff, min_verify_attempts, hard_cutoff),
            ).fetchall()
            for row in stale:
                looked = row["verify_attempts"] >= min_verify_attempts
                conn.execute(
                    "UPDATE send_intents SET status = 'unconfirmed' WHERE intent_id = ?",
                    (row["intent_id"],),
                )
                esc_id, new = self._open_escalation_in_txn(
                    conn,
                    row["thread_id"],
                    open_question=UNCONFIRMED_SEND_ASK if looked else UNCHECKED_SEND_ASK,
                    kind="unconfirmed_send",
                    context_summary=(
                        UNCONFIRMED_SEND_CONTEXT if looked else UNCHECKED_SEND_CONTEXT
                    ),
                    options=list(UNCONFIRMED_SEND_OPTIONS),
                )
                folded.append(
                    {
                        "intent_id": row["intent_id"],
                        "thread_id": row["thread_id"],
                        "escalation_id": esc_id,
                        "escalation_new": new,
                        "looked": looked,
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
