"""The tells nothing happens to write: a buyer left waiting on the seller's answer, a listing no
buyer has written about, and a sale the seller said they would close themselves. Each is a fact
about time passing, so the update lane writes them itself, once a minute, and each writer's
once-guard is the ledger: the tell it would write is already there.

Also here: the record of a sale handed to the seller, which those check-ins are written from, and
what the weekly line reads (what is live, and how many buyers wrote this week).
"""

from __future__ import annotations

from typing import TypedDict

from sellee.db import Database
from sellee.store.helpers import UNCONFIRMED_SEND_KIND, StoreError, _now
from sellee.store.updates import _QUOTE_MAX_CHARS, _buyer_said, _insert_tell
from sellee.updates import (
    TELL_ASK_WAITING,
    TELL_DEAL_CHECKIN,
    TELL_LISTING_QUIET,
    AskWaitingFacts,
    DealCheckinFacts,
    ListingQuietFacts,
)

# A buyer waiting this long on the seller's answer is told to them again, once per escalation.
ASK_WAITING_AFTER_SEC = 12 * 3600
# A listing live this long with no buyer is quiet, and told so at most this often.
LISTING_QUIET_AFTER_SEC = 7 * 24 * 3600
# A sale the seller is closing is asked about this long after they took it, and as often after.
DEAL_CHECKIN_EVERY_SEC = 24 * 3600
# The weekly line counts the buyers who wrote in this long.
WEEK_SEC = 7 * 24 * 3600


class LiveListing(TypedDict):
    """A listing up on at least one marketplace and not sold, as the weekly line names it."""

    item_id: str
    title: str
    live_since_ts: float


# Open escalations on the seller's listings past the wait, with no ask_waiting since they opened.
# Not one asking whether our own message landed: no buyer is waiting on the seller there, and
# calling it that invites an answer sent again over a message that may already have arrived. Nor
# one on a held thread: that is a suspected scam, and calling its sender a buyer waiting for an
# answer, quoting their bait and asking the seller to reply is the engagement the hold exists to
# stop. Releasing the thread clears the hold, so a false alarm is told like any other.
_ASK_WAITING_DUE_SQL = (
    "SELECT e.thread_id, t.item_id, e.created_ts, t.counterpart_handle, i.title "
    "FROM escalations e JOIN threads t ON t.thread_id = e.thread_id "
    "JOIN items i ON i.id = t.item_id "
    "WHERE e.status = 'open' AND t.side = 'sell' AND e.created_ts <= ? "
    "AND t.held_reason IS NULL AND COALESCE(e.kind, '') != ? "
    "AND NOT EXISTS (SELECT 1 FROM tells x WHERE x.kind = ? AND x.thread_id = e.thread_id "
    "  AND x.created_ts >= e.created_ts) "
    "ORDER BY e.created_ts, e.id"
)
# Listings live past the quiet mark, unsold, never written about, and not told quiet lately.
_LISTING_QUIET_DUE_SQL = (
    "SELECT i.id, i.title, i.live_since_ts FROM items i "
    "LEFT JOIN negotiations n ON n.item_id = i.id "
    "WHERE i.live_since_ts IS NOT NULL AND i.live_since_ts <= ? "
    "AND COALESCE(n.state, 'open') != 'sold' "
    "AND NOT EXISTS (SELECT 1 FROM threads t WHERE t.item_id = i.id AND t.side = 'sell') "
    "AND NOT EXISTS (SELECT 1 FROM tells x WHERE x.kind = ? AND x.item_id = i.id "
    "  AND x.created_ts > ?) "
    "ORDER BY i.live_since_ts, i.id"
)
# Sales handed to the seller a day ago or more, unsettled, on a listing still up (a check-in on one
# taken down could never be told), whose last check-in is a day old and was told: one still untold
# is the question the seller has not seen yet.
_DEAL_CHECKIN_DUE_SQL = (
    "SELECT d.thread_id, d.item_id, t.counterpart_handle, i.title "
    "FROM seller_handled_deals d JOIN threads t ON t.thread_id = d.thread_id "
    "JOIN items i ON i.id = d.item_id "
    "LEFT JOIN negotiations n ON n.item_id = d.item_id "
    "WHERE d.settled_ts IS NULL AND d.handed_ts <= ? AND COALESCE(n.state, 'open') != 'sold' "
    "AND i.live_since_ts IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM tells x WHERE x.kind = ? AND x.thread_id = d.thread_id "
    "  AND (x.created_ts > ? OR x.told_ts IS NULL)) "
    "ORDER BY d.handed_ts, d.thread_id"
)
# Untold slow tells whose premise is gone: the escalation answered or its thread held since, the
# sale settled, a buyer now writing about the listing.
_MOOT_SQL = (
    "UPDATE tells SET told_ts = ?, notice_id = NULL WHERE told_ts IS NULL AND ("
    " (kind = ? AND (NOT EXISTS (SELECT 1 FROM escalations e WHERE e.thread_id = tells.thread_id"
    "   AND e.status = 'open' AND e.created_ts <= tells.created_ts)"
    "   OR EXISTS (SELECT 1 FROM threads h WHERE h.thread_id = tells.thread_id"
    "   AND h.held_reason IS NOT NULL)))"
    " OR (kind = ? AND NOT EXISTS (SELECT 1 FROM seller_handled_deals d"
    "   WHERE d.thread_id = tells.thread_id AND d.settled_ts IS NULL))"
    " OR (kind = ? AND EXISTS (SELECT 1 FROM threads t WHERE t.item_id = tells.item_id"
    "   AND t.side = 'sell')))"
)


def _write_ask_waiting_in_txn(conn, now: float) -> int:
    rows = conn.execute(
        _ASK_WAITING_DUE_SQL, (now - ASK_WAITING_AFTER_SEC, UNCONFIRMED_SEND_KIND, TELL_ASK_WAITING)
    ).fetchall()
    for row in rows:
        facts: AskWaitingFacts = {
            "buyer_handle": row["counterpart_handle"],
            "buyer_said": _buyer_said(conn, row["thread_id"], None)[:_QUOTE_MAX_CHARS],
            "asked_ts": row["created_ts"],
            "item_title": row["title"],
        }
        _insert_tell(
            conn,
            item_id=row["item_id"],
            thread_id=row["thread_id"],
            kind=TELL_ASK_WAITING,
            decision=True,
            facts=facts,
            now=now,
        )
    return len(rows)


def _write_listing_quiet_in_txn(conn, now: float) -> int:
    since = now - LISTING_QUIET_AFTER_SEC
    rows = conn.execute(_LISTING_QUIET_DUE_SQL, (since, TELL_LISTING_QUIET, since)).fetchall()
    for row in rows:
        facts: ListingQuietFacts = {
            "live_since_ts": row["live_since_ts"],
            "item_title": row["title"],
        }
        _insert_tell(
            conn,
            item_id=row["id"],
            thread_id=None,
            kind=TELL_LISTING_QUIET,
            decision=False,
            facts=facts,
            now=now,
        )
    return len(rows)


def _write_deal_checkin_in_txn(conn, now: float) -> int:
    since = now - DEAL_CHECKIN_EVERY_SEC
    rows = conn.execute(_DEAL_CHECKIN_DUE_SQL, (since, TELL_DEAL_CHECKIN, since)).fetchall()
    for row in rows:
        facts: DealCheckinFacts = {
            "buyer_handle": row["counterpart_handle"],
            "item_title": row["title"],
        }
        _insert_tell(
            conn,
            item_id=row["item_id"],
            thread_id=row["thread_id"],
            kind=TELL_DEAL_CHECKIN,
            decision=True,
            facts=facts,
            now=now,
        )
    return len(rows)


def hand_deal_to_seller_in_txn(conn, escalation_id: str, *, now: float) -> None:
    """Record that the seller is closing the sale this escalation asked about themselves, so they
    are asked once a day whether it went through. Handing the same buyer's sale over again starts
    the count afresh. Only a sale on one of the seller's listings can be handed to them."""
    row = conn.execute(
        "SELECT t.thread_id, t.side, t.item_id FROM escalations e "
        "JOIN threads t ON t.thread_id = e.thread_id WHERE e.id = ?",
        (escalation_id,),
    ).fetchone()
    if row is None or row["side"] != "sell" or not row["item_id"]:
        raise StoreError(
            "only a sale on one of the seller's listings can be left to them — this escalation "
            "is not about a buyer on their listing"
        )
    conn.execute(
        "INSERT INTO seller_handled_deals (thread_id, item_id, escalation_id, handed_ts) "
        "VALUES (?, ?, ?, ?) ON CONFLICT (thread_id) DO UPDATE SET item_id = excluded.item_id, "
        "escalation_id = excluded.escalation_id, handed_ts = excluded.handed_ts, "
        "settled_ts = NULL",
        (row["thread_id"], row["item_id"], escalation_id, now),
    )


class SlowTellsMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

    def write_slow_tells(self, *, now: float | None = None) -> int:
        """Write every slow tell now due, in one transaction; how many were written. Run again over
        the same rows, it writes nothing."""
        at = now if now is not None else _now()
        with self._db.transaction() as conn:
            return (
                _write_ask_waiting_in_txn(conn, at)
                + _write_listing_quiet_in_txn(conn, at)
                + _write_deal_checkin_in_txn(conn, at)
            )

    def retire_moot_tells(self, *, now: float | None = None) -> int:
        """Retire, untold, every slow tell whose premise went away before it was told; how many."""
        at = now if now is not None else _now()
        with self._db.transaction() as conn:
            cur = conn.execute(
                _MOOT_SQL, (at, TELL_ASK_WAITING, TELL_DEAL_CHECKIN, TELL_LISTING_QUIET)
            )
            return cur.rowcount

    def live_listings(self) -> list[LiveListing]:
        """Every listing live somewhere and not sold, the most recently listed first."""
        rows = self._db.query(
            "SELECT i.id, i.title, i.live_since_ts FROM items i "
            "LEFT JOIN negotiations n ON n.item_id = i.id "
            "WHERE i.live_since_ts IS NOT NULL AND COALESCE(n.state, 'open') != 'sold' "
            "ORDER BY i.live_since_ts DESC, i.id"
        )
        return [
            {"item_id": r["id"], "title": r["title"], "live_since_ts": r["live_since_ts"]}
            for r in rows
        ]

    def seller_handled_threads(self, item_id: str) -> list[str]:
        """The threads of the sales on `item_id` the seller is closing themselves and has not
        settled, the earliest handed over first."""
        rows = self._db.query(
            "SELECT thread_id FROM seller_handled_deals "
            "WHERE item_id = ? AND settled_ts IS NULL ORDER BY handed_ts, thread_id",
            (item_id,),
        )
        return [r["thread_id"] for r in rows]

    def checkin_thread(self, item_id: str, notice_id: int) -> str | None:
        """The thread of the sale on `item_id` the check-in told by `notice_id` asked about, or
        None when that notice told no check-in on the item. An update asks about one sale, so a
        tap on its buttons answers for that buyer, whoever the seller has taken over since."""
        rows = self._db.query(
            "SELECT thread_id FROM tells WHERE kind = ? AND notice_id = ? AND item_id = ? "
            "ORDER BY id LIMIT 1",
            (TELL_DEAL_CHECKIN, notice_id, item_id),
        )
        return rows[0]["thread_id"] if rows else None

    def buyers_who_wrote_since(self, since: float) -> int:
        """How many buyers on the seller's live listings (as `live_listings` reads them) wrote at
        or after `since`: the weekly line names only those, so a buyer of one since sold or taken
        down would read as someone writing about what is still up."""
        rows = self._db.query(
            "SELECT COUNT(DISTINCT m.thread_id) AS n FROM thread_messages m "
            "JOIN threads t ON t.thread_id = m.thread_id "
            "JOIN items i ON i.id = t.item_id "
            "LEFT JOIN negotiations n ON n.item_id = i.id "
            "WHERE t.side = 'sell' AND m.dir = 'in' AND m.ts >= ? "
            "AND i.live_since_ts IS NOT NULL AND COALESCE(n.state, 'open') != 'sold'",
            (since,),
        )
        return rows[0]["n"]
