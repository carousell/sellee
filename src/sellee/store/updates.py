"""The ledger activity updates are composed from: every thing done on a seller's listing that they
have not been told about yet (a "tell").

Each tell is written by the transaction that produced the fact, through the in-transaction writers
here, so a tell can never describe something that was rolled back and never goes missing for
something that committed. The floor stays out by construction: the writers are handed it only to
say whether an offer was under it, and no column or facts shape has a place for the value.
"""

from __future__ import annotations

import json
from typing import TypedDict

from sellee.db import Database
from sellee.store.helpers import _now

TELL_BUYER_ANSWERED = "buyer_answered"
TELL_OFFER_DECLINED = "offer_declined"
# The engine's decisions that leave nothing for a lower minimum to win back: the offer was taken or
# leads, or it was never weighed on price (a sold item, an item another buyer holds, an auction it
# did not lead). Any other decision under the floor turned the buyer's number down on its price and
# is the seller's to decide on, so a decision the engine adds later asks the seller by default.
_NOT_A_PRICE_DECLINE = frozenset({"accept_fcfs", "bid_lead", "sold", "fcfs_taken", "bid_outbid"})
# A tell quotes at most this much of either side's message, so a long message cannot bloat the row;
# a line to the seller quotes far less.
_QUOTE_MAX_CHARS = 500


class OfferDeclinedFacts(TypedDict):
    """An offer under the seller's minimum that was not taken — the buyer's number, never the
    minimum it fell under."""

    buyer_handle: str
    amount: float
    currency: str
    item_title: str


class BuyerAnsweredFacts(TypedDict):
    """A buyer we answered: their last words, ours, and whether they are the item's first buyer —
    no other buyer on it had been answered before them.

    `buyer_said` and `we_said` are the messages as sent, cut only to a bounded length: a buyer's
    words can carry links, emails and phone numbers. They are not fit to quote to the seller until
    the contact details are stripped and the text is clipped to a sentence."""

    buyer_handle: str
    buyer_said: str
    we_said: str
    first_on_item: bool
    item_title: str


class TellRecord(TypedDict):
    """One untold `tells` row. `decision` says the line asks the seller for something."""

    id: int
    item_id: str | None
    thread_id: str | None
    kind: str
    decision: bool
    facts: OfferDeclinedFacts | BuyerAnsweredFacts
    created_ts: float


def _tell_from_row(row) -> TellRecord:
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "thread_id": row["thread_id"],
        "kind": row["kind"],
        "decision": bool(row["decision"]),
        "facts": json.loads(row["facts"]),
        "created_ts": row["created_ts"],
    }


def _insert_tell(
    conn,
    *,
    item_id: str,
    thread_id: str,
    kind: str,
    decision: bool,
    facts: OfferDeclinedFacts | BuyerAnsweredFacts,
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

    def mark_tells_told(
        self, tell_ids: list[int], *, notice_id: int | None, now: float | None = None
    ) -> int:
        """Stamp tells as told by `notice_id`, or retired untold when it is None. A tell already
        stamped keeps its first stamp, so a retried caller cannot re-point it. Returns how many
        were stamped by this call."""
        if not tell_ids:
            return 0
        stamped_ts = now if now is not None else _now()
        placeholders = ", ".join("?" for _ in tell_ids)
        with self._db.transaction() as conn:
            cur = conn.execute(
                f"UPDATE tells SET told_ts = ?, notice_id = ? "
                f"WHERE told_ts IS NULL AND id IN ({placeholders})",
                (stamped_ts, notice_id, *tell_ids),
            )
            return cur.rowcount
