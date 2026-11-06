"""The ledger activity updates are composed from: every thing done on a seller's listing that they
have not been told about yet (a "tell").

Each tell is written by the transaction that produced the fact, through the in-transaction writers
here, so a tell can never describe something that was rolled back and never goes missing for
something that committed. The floor stays out by construction: the writers are handed it only to
say whether an offer was under it, and no column or facts shape has a place for the value.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import cast

from sellee.db import Database
from sellee.store.helpers import _REPLY_THREAD_STATUSES, _insert_ask, _insert_notice, _now
from sellee.updates import (
    TELL_BUYER_ANSWERED,
    TELL_OFFER_DECLINED,
    BuyerAnsweredFacts,
    OfferDeclinedFacts,
    TellFacts,
    TellRecord,
    UpdateContext,
)

# The engine's decisions that leave nothing for a lower minimum to win back: the offer was taken or
# leads, or it was never weighed on price (a sold item, an item another buyer holds, an auction it
# did not lead). Any other decision under the floor turned the buyer's number down on its price and
# is the seller's to decide on, so a decision the engine adds later asks the seller by default.
_NOT_A_PRICE_DECLINE = frozenset({"accept_fcfs", "bid_lead", "sold", "fcfs_taken", "bid_outbid"})
# A tell quotes at most this much of either side's message, so a long message cannot bloat the row;
# a line to the seller quotes far less.
_QUOTE_MAX_CHARS = 500


def _facts(raw: str | bytes | None) -> TellFacts | None:
    """A row's facts as stored, or None when they do not parse as a record — the composer retires
    a tell it cannot read rather than fail on it, so one bad row never stops every update after it.
    A record is all this checks; the composer checks each field before it reads one."""
    try:
        parsed = json.loads(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return cast("TellFacts", parsed) if isinstance(parsed, dict) else None


def _tell_from_row(row) -> TellRecord:
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "thread_id": row["thread_id"],
        "kind": row["kind"],
        "decision": bool(row["decision"]),
        "facts": _facts(row["facts"]),
        "created_ts": row["created_ts"],
    }


def _insert_tell(
    conn,
    *,
    item_id: str,
    thread_id: str,
    kind: str,
    decision: bool,
    facts: TellFacts,
    now: float,
) -> None:
    conn.execute(
        "INSERT INTO tells (item_id, thread_id, kind, decision, facts, created_ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (item_id, thread_id, kind, 1 if decision else 0, json.dumps(facts, sort_keys=True), now),
    )


def _item_title(conn, item_id: str) -> str:
    row = conn.execute("SELECT title FROM items WHERE id = ?", (item_id,)).fetchone()
    return row["title"] if row else ""


def record_offer_decision_in_txn(
    conn,
    *,
    item_id: str,
    thread_id: str,
    handle: str,
    offer: float,
    currency: str | None,
    result: dict,
    floor: float,
) -> None:
    """Owe the seller a decision when a decided offer fell under their floor and was turned down on
    price. `floor` is read only for that comparison.

    A buyer has at most one question waiting for the seller on an item: whatever their newest
    offer came to, it retires any earlier one still waiting to be told, untold. A buyer walking up
    under the floor is one question rather than one per round, and a buyer whose later offer met
    the floor, or was turned away for a reason other than price, is no question at all."""
    now = _now()
    below_floor = offer < floor
    decision = result["decision"]
    conn.execute(
        "UPDATE tells SET told_ts = ?, notice_id = NULL WHERE kind = ? AND item_id = ? "
        "AND thread_id = ? AND told_ts IS NULL",
        (now, TELL_OFFER_DECLINED, item_id, thread_id),
    )
    if not below_floor or decision in _NOT_A_PRICE_DECLINE:
        return
    facts: OfferDeclinedFacts = {
        "buyer_handle": handle,
        "amount": offer,
        "currency": currency or "",
        "item_title": _item_title(conn, item_id),
    }
    _insert_tell(
        conn,
        item_id=item_id,
        thread_id=thread_id,
        kind=TELL_OFFER_DECLINED,
        decision=True,
        facts=facts,
        now=now,
    )


def _buyer_said(conn, thread_id: str, answered_msg_id: str | None) -> str:
    """The buyer message this reply answered, or else the buyer's newest one on the thread; empty
    only when the buyer has said nothing there at all."""
    row = None
    if answered_msg_id is not None:
        row = conn.execute(
            "SELECT text FROM thread_messages WHERE thread_id = ? AND msg_id = ? AND dir = 'in'",
            (thread_id, answered_msg_id),
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT text FROM thread_messages WHERE thread_id = ? AND dir = 'in' "
            "ORDER BY ts DESC, rowid DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
    return row["text"] if row else ""


# When this buyer was first really answered. The reply being committed is already stamped, so
# this is never later than now.
_FIRST_ANSWER_SQL = (
    "SELECT MIN(committed_ts) AS ts FROM send_intents "
    "WHERE thread_id = ? AND kind = 'reply' AND status = 'committed'"
)
# Runs inside every reply commit, so it starts from the item's few threads and reaches each one's
# sends by index, rather than walking every committed send there has ever been.
_RIVAL_ANSWERED_SQL = (
    "SELECT 1 FROM threads t WHERE t.item_id = ? AND t.side = 'sell' AND t.thread_id != ? "
    "AND EXISTS (SELECT 1 FROM send_intents si WHERE si.thread_id = t.thread_id "
    "AND si.kind = 'reply' AND si.status = 'committed' AND si.committed_ts < ?) LIMIT 1"
)


def _another_buyer_answered_first(conn, item_id: str, thread_id: str, now: float) -> bool:
    """Whether a buyer on another thread of this item had a real answer before this buyer's first
    one. A holding line or a follow-up answered nothing, and a rival answered after this buyer does
    not make them any less the first, however often either is answered later."""
    first = conn.execute(_FIRST_ANSWER_SQL, (thread_id,)).fetchone()
    first_ts = first["ts"] if first is not None and first["ts"] is not None else now
    row = conn.execute(_RIVAL_ANSWERED_SQL, (item_id, thread_id, first_ts)).fetchone()
    return row is not None


def record_buyer_answered_in_txn(
    conn, *, thread_id: str, answered_msg_id: str | None, we_said: str, now: float
) -> None:
    """Owe the seller an FYI that a buyer on one of their listings was answered. A buying-side
    thread is not about the seller's listing and tells nothing. With no buyer message behind the
    reply, what the buyer said is left empty rather than the answer going untold."""
    thread = conn.execute(
        "SELECT side, item_id, counterpart_handle FROM threads WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    if thread is None or thread["side"] != "sell" or not thread["item_id"]:
        return
    item_id = thread["item_id"]
    facts: BuyerAnsweredFacts = {
        "buyer_handle": thread["counterpart_handle"],
        "buyer_said": _buyer_said(conn, thread_id, answered_msg_id)[:_QUOTE_MAX_CHARS],
        "we_said": we_said[:_QUOTE_MAX_CHARS],
        "first_on_item": not _another_buyer_answered_first(conn, item_id, thread_id, now),
        "item_title": _item_title(conn, item_id),
    }
    _insert_tell(
        conn,
        item_id=item_id,
        thread_id=thread_id,
        kind=TELL_BUYER_ANSWERED,
        decision=False,
        facts=facts,
        now=now,
    )


class UpdatesMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

    def untold_tells(self) -> list[TellRecord]:
        """Every tell not yet carried by a notice or retired, oldest first."""
        rows = self._db.query(
            "SELECT * FROM tells WHERE told_ts IS NULL ORDER BY created_ts, id",
        )
        return [_tell_from_row(r) for r in rows]

    def last_update_ts(self) -> float | None:
        """When the latest update told anything, or None when none ever has: the time its tells
        were stamped told by it, which is the clock the hold is measured on."""
        row = self._db.query("SELECT MAX(told_ts) AS ts FROM tells WHERE notice_id IS NOT NULL")
        return row[0]["ts"] if row else None

    def mark_tells_told(
        self, tell_ids: list[int], *, notice_id: int | None, now: float | None = None
    ) -> int:
        """Stamp tells as told by `notice_id`, or retired untold when it is None. A tell already
        stamped keeps its first stamp, so a retried caller cannot re-point it. Returns how many
        were stamped by this call."""
        if not tell_ids:
            return 0
        stamped_ts = now if now is not None else _now()
        with self._db.transaction() as conn:
            return _stamp_tells_in_txn(conn, tell_ids, notice_id=notice_id, now=stamped_ts)

    def queue_update_notice(
        self,
        text: str,
        *,
        ref: str,
        tell_ids: Sequence[int],
        options: Sequence[str] = (),
        holdable: bool = True,
        now: float | None = None,
    ) -> int | None:
        """Queue the notice carrying an update and stamp every tell it carries told by it, in one
        transaction: a crash leaves both or neither, so a tell is never told twice or lost between
        the two. The notice carries `options` as tappable answers when it asks the seller
        something, and is `holdable` (it waits out quiet hours) unless the seller asked for it.
        None, writing nothing, when the update is no longer the one to send (see
        `_is_still_untold`)."""
        with self._db.transaction() as conn:
            if not _is_still_untold(conn, ref, tell_ids):
                return None
            if options:
                notice_id = _insert_ask(
                    conn, text, options=list(options), ref=ref, holdable=holdable
                )
            else:
                notice_id = _insert_notice(conn, text, ref=ref, holdable=holdable)
            _stamp_tells_in_txn(conn, tell_ids, notice_id=notice_id, now=_or_now(now))
            return notice_id

    def record_update_at_catchup(
        self, text: str, *, ref: str, tell_ids: Sequence[int], now: float | None = None
    ) -> int | None:
        """Record an update handed to the seller by a catchup, as a notice already delivered that
        way, and stamp every tell it carries told by it, in one transaction. No buttons: nothing
        is on a chat to tap, so its question is answered in words. None, writing nothing, when the
        update is no longer the one to send (see `_is_still_untold`)."""
        stamped_ts = _or_now(now)
        with self._db.transaction() as conn:
            if not _is_still_untold(conn, ref, tell_ids):
                return None
            notice_id = _insert_notice(conn, text, ref=ref)
            conn.execute(
                "UPDATE notices SET status = 'delivered', delivered_ts = ?, via = 'catchup' "
                "WHERE id = ?",
                (stamped_ts, notice_id),
            )
            _stamp_tells_in_txn(conn, tell_ids, notice_id=notice_id, now=stamped_ts)
            return notice_id

    def update_context(
        self, *, item_ids: Sequence[str], thread_ids: Sequence[str]
    ) -> UpdateContext:
        """What an update's words depend on beyond its tells, as of now: which of `item_ids` are
        still live (listed somewhere and not sold), and where each of `thread_ids` stands —
        talking when its buyer wrote last, waiting when the conversation is still ours to carry
        and the next word is theirs — and when our latest reply on each was sent."""
        talking, waiting = self._thread_standing(thread_ids)
        return {
            "live_item_ids": self._live_item_ids(item_ids),
            "talking_thread_ids": talking,
            "waiting_thread_ids": waiting,
            "replied_ts": self._replied_ts(thread_ids),
        }

    def _replied_ts(self, thread_ids: Sequence[str]) -> dict[str, float]:
        """When our latest reply landed on each of `thread_ids` that has one. A holding line or a
        follow-up answers nothing, and a reply still being sent has not reached the buyer."""
        wanted = sorted(set(thread_ids))
        if not wanted:
            return {}
        rows = self._db.query(
            "SELECT thread_id, MAX(committed_ts) AS ts FROM send_intents "
            f"WHERE thread_id IN ({_placeholders(wanted)}) AND kind = 'reply' "
            "AND status = 'committed' GROUP BY thread_id",
            tuple(wanted),
        )
        return {r["thread_id"]: r["ts"] for r in rows if r["ts"] is not None}

    def _live_item_ids(self, item_ids: Sequence[str]) -> list[str]:
        wanted = sorted(set(item_ids))
        if not wanted:
            return []
        rows = self._db.query(
            "SELECT i.id, i.listing_urls, n.state FROM items i "
            "LEFT JOIN negotiations n ON n.item_id = i.id "
            f"WHERE i.id IN ({_placeholders(wanted)}) ORDER BY i.id",
            tuple(wanted),
        )
        return [
            r["id"]
            for r in rows
            if any(json.loads(r["listing_urls"]).values()) and r["state"] != "sold"
        ]

    def _thread_standing(self, thread_ids: Sequence[str]) -> tuple[list[str], list[str]]:
        """(talking, waiting) among the threads in a reply status with no escalation open; a held,
        escalated or closed thread is in neither. Talking is a thread whose buyer wrote last: a
        message past the reply cursor, so an out-of-office the cursor was moved over is not one,
        and no reply from the seller by hand after it. A reply of ours still being sent does not
        count until it lands, so such a thread is talking even though the reply lane is not
        answering it again. Waiting is the rest."""
        wanted = sorted(set(thread_ids))
        if not wanted:
            return [], []
        rows = self._db.query(
            _THREAD_STANDING_SQL.format(
                wanted=_placeholders(wanted), statuses=_placeholders(_REPLY_THREAD_STATUSES)
            ),
            (*wanted, *_REPLY_THREAD_STATUSES),
        )
        talking = [r["thread_id"] for r in rows if r["buyer_wrote_last"]]
        waiting = [r["thread_id"] for r in rows if not r["buyer_wrote_last"]]
        return talking, waiting


# Each asked thread still ours to carry, and whether its buyer wrote last.
_THREAD_STANDING_SQL = (
    "SELECT t.thread_id, ("
    "  EXISTS (SELECT 1 FROM thread_messages m WHERE m.thread_id = t.thread_id "
    "    AND m.dir = 'in' AND (t.cursor_last_ts IS NULL OR m.ts > t.cursor_last_ts)) "
    "  AND NOT EXISTS (SELECT 1 FROM thread_messages ms WHERE ms.thread_id = t.thread_id "
    "    AND ms.dir = 'out' AND ms.source = 'manual' "
    "    AND ms.ts > (SELECT MAX(mi.ts) FROM thread_messages mi "
    "                 WHERE mi.thread_id = t.thread_id AND mi.dir = 'in'))"
    ") AS buyer_wrote_last "
    "FROM threads t "
    "WHERE t.thread_id IN ({wanted}) AND t.status IN ({statuses}) "
    "AND NOT EXISTS (SELECT 1 FROM escalations e WHERE e.thread_id = t.thread_id "
    "  AND e.status = 'open') "
    "ORDER BY t.thread_id"
)


def _placeholders(values: Sequence[object]) -> str:
    return ", ".join("?" for _ in values)


def _or_now(now: float | None) -> float:
    return now if now is not None else _now()


def _is_still_untold(conn, ref: str, tell_ids: Sequence[int]) -> bool:
    """Whether an update composed from an earlier read can still go: no notice under its `ref`, and
    every tell it carries still untold. The lane and a catchup read on different threads and can
    pick different sets with different refs, so the ref alone does not stop the second of them
    telling a tell the first already told; checking inside the writing transaction does, and the
    caller's next read composes from what is left."""
    if conn.execute("SELECT 1 FROM notices WHERE ref = ? LIMIT 1", (ref,)).fetchone() is not None:
        return False
    carried = tuple(sorted(set(tell_ids)))
    if not carried:
        return True
    row = conn.execute(
        f"SELECT COUNT(*) FROM tells WHERE told_ts IS NULL AND id IN ({_placeholders(carried)})",
        carried,
    ).fetchone()
    return row[0] == len(carried)


def _stamp_tells_in_txn(conn, tell_ids: Sequence[int], *, notice_id: int | None, now: float) -> int:
    """Stamp the tells among `tell_ids` not yet stamped as told by `notice_id` (None: retired
    untold), returning how many it stamped. One already stamped keeps its first stamp."""
    if not tell_ids:
        return 0
    cur = conn.execute(
        f"UPDATE tells SET told_ts = ?, notice_id = ? "
        f"WHERE told_ts IS NULL AND id IN ({_placeholders(tell_ids)})",
        (now, notice_id, *tell_ids),
    )
    return cur.rowcount
