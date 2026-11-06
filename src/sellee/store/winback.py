"""A lower floor goes back to the buyer it now closes a deal with.

A buyer whose best offer was under the old floor and meets the new one is reopened in the
transaction that lowered it, but only when the engine would accept that offer now: a rival standing
at or above it still outranks it, and a buyer reopened only to be countered or held firm would get
a message they never asked for. Their ledger row is marked so their next decided offer is weighed
against the new floor rather than answered as a repeat, and their thread is owed a reply the reply
lane claims as if they had written. A thread the seller has taken over (a reply of their own since
the buyer last wrote, or an escalation still open) is left to them, as the reply lane leaves it.
A floor raised again before that reply goes out takes the reopening back from the buyers it no
longer reaches.

The floor itself is only compared here, never written anywhere. The reply pass learns a thread is
owed from `owed_offer`, which carries the buyer's own best offer and nothing about why it is owed,
so nothing it is shown can tell a buyer the seller's minimum moved.

An owing is settled only by something that answered it: a reply whose sender was shown the owing
(a reply pass that claimed the thread while it was owed, or that took the decision it was owed
after it began; or any other sender that reserved its reply after the owing began), a hold that
handles the thread, or a reply the seller or the marketplace made after it. One made moot lapses
instead, in the ledger write that made it so: a reopening not yet decided once its buyer backs out,
loses, or is held for, or the item stops taking offers; any owing once the buyer backs out or
loses, or the item sells.
"""

from __future__ import annotations

import copy
import json
from typing import TypedDict

from sellee.db import Database
from sellee.engines import negotiate as negotiate_engine
from sellee.store.helpers import (
    _LIVE_OWING_SQL,
    _NO_MANUAL_REPLY_SINCE_BUYER_SQL,
    _NO_OPEN_ESCALATION_SQL,
    _REPLY_THREAD_STATUSES,
)
from sellee.store.negotiation import load_ledger_in_txn, take_back_reopening_in_txn
from sellee.store.updates import retire_offer_question_in_txn

# Buyers still in the running, or walked away from on price; one who withdrew or lost is not.
_REOPENABLE_STATUSES = ("active", "passed")

# Highest offer first, and the earlier buyer first on a tie, so the buyer a lower floor goes back to
# is the one a rival-aware engine would take.
_REACHED_SQL = (
    "SELECT nb.thread_id FROM negotiation_buyers nb "
    "JOIN threads t ON t.thread_id = nb.thread_id "
    "WHERE nb.item_id = ? AND nb.reopened = 0 "
    "AND nb.status IN ({statuses}) AND nb.highest_offer >= ? AND nb.highest_offer < ? "
    "AND t.side = 'sell' AND t.item_id = nb.item_id AND t.status IN ({talking}) "
    "AND " + _NO_MANUAL_REPLY_SINCE_BUYER_SQL + " "
    "AND " + _NO_OPEN_ESCALATION_SQL + " "
    "ORDER BY nb.highest_offer DESC, nb.rowid ASC"
).format(
    statuses=",".join("?" for _ in _REOPENABLE_STATUSES),
    talking=",".join("?" for _ in _REPLY_THREAD_STATUSES),
)


class OwedOffer(TypedDict):
    """The offer a thread owed a reply is waiting on a fresh decision for: the buyer's own best
    offer, in the item's currency. Never the floor, and nothing about why it is owed."""

    offer: float
    currency: str | None


class WinbackMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

    def owed_offer(self, thread_id: str) -> OwedOffer | None:
        """The buyer's best offer when the thread is owed a reply for it, else None."""
        rows = self._db.query(
            "SELECT nb.highest_offer, i.currency FROM threads t "
            "JOIN negotiation_buyers nb ON nb.thread_id = t.thread_id AND nb.item_id = t.item_id "
            "JOIN items i ON i.id = t.item_id "
            "WHERE t.thread_id = ? AND nb.highest_offer > 0 AND "
            + _LIVE_OWING_SQL
            + " IS NOT NULL",
            (thread_id,),
        )
        if not rows:
            return None
        return {"offer": rows[0]["highest_offer"], "currency": rows[0]["currency"]}


def reopen_for_floor_change_in_txn(
    conn, item_id: str, *, old_floor: float | None, new_floor: float, now: float
) -> int:
    """Reopen the buyers a floor change from `old_floor` to `new_floor` now closes a deal with,
    returning how many. Nothing when the floor did not go down, or when the item is not open to
    offers (sold, held for a buyer, or in an auction), since no offer under list could be taken
    then. A buyer already reopened and not yet decided again is not reopened twice. A floor that
    went up instead takes the reopening back from the buyers whose offer it no longer reaches."""
    if old_floor is None:
        return 0
    if new_floor > old_floor:
        _withdraw_reopenings(conn, item_id, new_floor)
        return 0
    if new_floor == old_floor or not _open_to_offers(conn, item_id):
        return 0
    rows = conn.execute(
        _REACHED_SQL,
        (item_id, *_REOPENABLE_STATUSES, new_floor, old_floor, *_REPLY_THREAD_STATUSES),
    ).fetchall()
    if not rows:
        return 0
    led = load_ledger_in_txn(conn, item_id)
    list_price, knobs = _pricing(conn, item_id)
    reopened = 0
    for row in rows:
        trial = _standing_again(led, row["thread_id"])
        if not _accepted(trial, row["thread_id"], new_floor, list_price, knobs):
            continue
        # The buyer stands again, so every later candidate is weighed against their offer too.
        led = trial
        _reopen(conn, item_id, row["thread_id"], now)
        reopened += 1
    return reopened


def owing_seen_in_txn(conn, thread_id: str, pass_id: str | None) -> float | None:
    """The owing a reply being reserved now was composed with sight of, or None.

    A reply pass saw what its claim recorded, which is earlier than now: a thread that became owed
    while the pass was composing was never shown to it, unless the pass has since taken the decision
    the owing waits for (the reopening is spent), in which case its reply carries that decision.
    Any other sender (the seller's own session, or a channel pass carrying their words) is
    reserving with the thread as it stands."""
    if pass_id is not None:
        row = conn.execute(
            "SELECT type, payload FROM passes WHERE pass_id = ?", (pass_id,)
        ).fetchone()
        if row is not None and row["type"] == "reply":
            claimed = (json.loads(row["payload"]) or {}).get("claimed_owed") or {}
            if thread_id in claimed:
                return claimed[thread_id]
            return _owing_already_decided(conn, thread_id)
    row = conn.execute(
        "SELECT reply_owed_ts FROM threads WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    return row["reply_owed_ts"] if row else None


def settle_owed_reply_in_txn(conn, thread_id: str, *, through_ts: float | None) -> None:
    """The thread was answered by something that came at or after `through_ts`: an owing that
    began no later than that is settled. A None `through_ts` settles nothing."""
    if through_ts is None:
        return
    conn.execute(
        "UPDATE threads SET reply_owed_ts = NULL WHERE thread_id = ? "
        "AND reply_owed_ts IS NOT NULL AND reply_owed_ts <= ?",
        (thread_id, through_ts),
    )


def _owing_already_decided(conn, thread_id: str) -> float | None:
    """The thread's owing when the decision it waits for has already been taken: only a reopening
    sets an owing, and only a decided offer spends one while leaving the owing in place."""
    row = conn.execute(
        "SELECT t.reply_owed_ts FROM threads t "
        "JOIN negotiation_buyers nb ON nb.thread_id = t.thread_id AND nb.item_id = t.item_id "
        "WHERE t.thread_id = ? AND t.reply_owed_ts IS NOT NULL AND nb.reopened = 0",
        (thread_id,),
    ).fetchone()
    return row["reply_owed_ts"] if row else None


def _open_to_offers(conn, item_id: str) -> bool:
    row = conn.execute("SELECT state FROM negotiations WHERE item_id = ?", (item_id,)).fetchone()
    return row is not None and row["state"] == "open"


def _pricing(conn, item_id: str) -> tuple:
    """The list price and the counter knobs the engine weighs an offer with. The seller's
    firmness and the install's counter settings only shape a counter, never whether an offer that
    meets the minimum is accepted, so the defaults stand in for them here."""
    item = conn.execute("SELECT list_price FROM items WHERE id = ?", (item_id,)).fetchone()
    floor_row = conn.execute(
        "SELECT auto_counter_step, auto_counter_rounds FROM floors WHERE item_id = ?", (item_id,)
    ).fetchone()
    record = dict(floor_row) if floor_row else {}
    return item["list_price"], negotiate_engine.resolve_knobs(None, record)


def _standing_again(led: dict, thread_id: str) -> dict:
    """The ledger with this buyer standing again, as a reopening would leave it."""
    trial = copy.deepcopy(led)
    trial["buyers"][thread_id]["status"] = "active"
    return trial


def _accepted(led: dict, thread_id: str, floor: float, list_price, knobs: dict) -> bool:
    """Whether the engine, deciding this buyer's best offer as a reopened one, accepts it."""
    buyer = copy.deepcopy(led["buyers"][thread_id])
    res = negotiate_engine.decide(
        buyer["highest_offer"],
        thread_id,
        buyer,
        led,
        floor,
        list_price,
        knobs["step"],
        knobs,
        reopened=True,
    )
    return res["decision"] == "accept_fcfs"


def _reopen(conn, item_id: str, thread_id: str, now: float) -> None:
    conn.execute(
        "UPDATE negotiation_buyers SET reopened_from = status, status = 'active', reopened = 1 "
        "WHERE item_id = ? AND thread_id = ?",
        (item_id, thread_id),
    )
    conn.execute(
        "UPDATE threads SET reply_owed_ts = ?, updated_ts = ? WHERE thread_id = ?",
        (now, now, thread_id),
    )
    # The offer-under-the-minimum question it left for the seller is answered by this change.
    retire_offer_question_in_txn(conn, item_id=item_id, thread_id=thread_id, now=now)


def _withdraw_reopenings(conn, item_id: str, new_floor: float) -> None:
    """Take a reopening back from each buyer the raised floor no longer reaches. Their
    offer-under-the-minimum question stays retired: the raised floor is the seller's answer to
    it."""
    rows = conn.execute(
        "SELECT thread_id FROM negotiation_buyers "
        "WHERE item_id = ? AND reopened = 1 AND highest_offer < ?",
        (item_id, new_floor),
    ).fetchall()
    for row in rows:
        take_back_reopening_in_txn(conn, item_id, row["thread_id"])
