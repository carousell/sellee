"""The listings a seller already had: what a survey found, what they said about it, and what is
still owed on it.

Two tables, one lifecycle: a market is surveyed once (the primary key is the market, and the
triggers insert without overwriting), what it found becomes `discovered_listings` rows, and an
accepted row becomes an item.

Two writers are transactional compositions rather than plain accessors, and that is the point of
this module:

  * `record_survey_result` records what was found, closes the survey, and queues the ask in one
    transaction — split in two, a crash leaves rows nobody was ever asked about behind a one-ask
    guard that has already closed.
  * `adopt_discovered_listing` creates the item **carrying its marketplace URL** and advances the
    row together — an item with no URL is an orphan a retry duplicates, and a URL with no advanced
    row never gets published.

Everything else is bookkeeping the lane reads back — including `rail_state`, the durable record that
a carousell.ai publish is still owed. Nothing else could recover one, so a publish skipped because
the slot was busy is remembered here or it is lost.
"""

from __future__ import annotations

import json
from typing import TypedDict

from sellee.db import Database
from sellee.store.helpers import (
    ItemRecord,
    StoreError,
    _forget_thread_listings_in_txn,
    _insert_item_in_txn,
    _insert_notice,
    _item_from_row,
    _now,
    validate_photos,
)


class ListingGone(StoreError):
    """The row stopped waiting to be adopted while it was being adopted — declined from the chat, or
    cleared by a fresh look. Not a failure of the listing: the lane drops it quietly."""


# A survey owes a look, has had one, or has been given up on. "Gave up" must never read as
# "nothing listed" — one says we cannot see the market, the other says the seller has nothing on it.
SURVEY_DUE = "due"
SURVEY_DONE = "done"
SURVEY_ABANDONED = "abandoned"

# Where a discovered listing is in its life. `pending` is asked-and-waiting; `accepted`/`declined`
# are the seller's answer; `expired` is an ask that went stale unanswered; `adopted` and `failed`
# are terminal.
LISTING_PENDING = "pending"
LISTING_ACCEPTED = "accepted"
LISTING_DECLINED = "declined"
LISTING_EXPIRED = "expired"
LISTING_ADOPTED = "adopted"
LISTING_FAILED = "failed"

# What the seller asked us to do with an accepted listing: answer buyers on it, or also put it on
# carousell.ai.
MANAGE_INBOX = "inbox"
MANAGE_RELIST = "relist"
MANAGE_MODES = (MANAGE_INBOX, MANAGE_RELIST)

# The carousell.ai publish an adopted listing is owed: queued behind the one slot, in flight,
# landed, or given up on after its retries.
RAIL_OWED = "owed"
RAIL_QUEUED = "queued"
RAIL_DONE = "done"
RAIL_FAILED = "failed"


class MarketSurvey(TypedDict):
    market: str
    state: str
    requested_ts: float
    surveyed_ts: float | None
    attempts: int
    found: int


class DiscoveredListing(TypedDict):
    market: str
    listing_id: str
    url: str
    title: str
    price: float
    price_text: str
    status: str
    manage: str | None
    attempts: int
    last_error: str | None
    item_id: str | None
    rail_state: str | None
    rail_pass_id: str | None
    rail_attempts: int
    discovered_ts: float
    decided_ts: float | None
    adopted_ts: float | None


def _survey_from_row(row) -> MarketSurvey:
    return MarketSurvey(
        market=row["market"],
        state=row["state"],
        requested_ts=row["requested_ts"],
        surveyed_ts=row["surveyed_ts"],
        attempts=row["attempts"],
        found=row["found"],
    )


def _listing_from_row(row) -> DiscoveredListing:
    return DiscoveredListing(
        market=row["market"],
        listing_id=row["listing_id"],
        url=row["url"],
        title=row["title"],
        price=row["price"],
        price_text=row["price_text"],
        status=row["status"],
        manage=row["manage"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        item_id=row["item_id"],
        rail_state=row["rail_state"],
        rail_pass_id=row["rail_pass_id"],
        rail_attempts=row["rail_attempts"],
        discovered_ts=row["discovered_ts"],
        decided_ts=row["decided_ts"],
        adopted_ts=row["adopted_ts"],
    )


class SurveyMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

    # --- the survey ---------------------------------------------------------------------------

    def request_market_survey(self, market: str) -> bool:
        """Ask the survey lane to look at what the seller already has on `market`. Returns whether
        this created the request.

        Insert-only: a market that has already been surveyed (or given up on) keeps its row, which
        is what makes the ask happen once. Both triggers call this unconditionally and let the
        primary key decide.
        """
        with self._db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO market_surveys (market, state, requested_ts) "
                "VALUES (?, 'due', ?) ON CONFLICT (market) DO NOTHING",
                (market, _now()),
            )
            return cur.rowcount > 0

    def pending_market_surveys(self) -> list[MarketSurvey]:
        """Every market still owed a look, oldest request first."""
        rows = self._db.query(
            "SELECT * FROM market_surveys WHERE state = 'due' ORDER BY requested_ts ASC, market ASC"
        )
        return [_survey_from_row(r) for r in rows]

    def get_market_survey(self, market: str) -> MarketSurvey | None:
        rows = self._db.query("SELECT * FROM market_surveys WHERE market = ?", (market,))
        return _survey_from_row(rows[0]) if rows else None

    def bump_survey_attempt(self, market: str) -> int:
        """Count one look that could not be served — signed out, or a listings page we could not
        read. Returns the new count."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE market_surveys SET attempts = attempts + 1 WHERE market = ?", (market,)
            )
            row = conn.execute(
                "SELECT attempts FROM market_surveys WHERE market = ?", (market,)
            ).fetchone()
        return row["attempts"] if row else 0

    def abandon_market_survey(self, market: str) -> None:
        """Stop looking at a market whose listings we cannot read. Not 'done': this market was never
        surveyed, and `found = 0` would read as "nothing listed"."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE market_surveys SET state = 'abandoned', surveyed_ts = ? WHERE market = ?",
                (_now(), market),
            )

    def reopen_market_survey(self, market: str) -> None:
        """Take a fresh look at a market that has already been surveyed — the one door back through
        the one-ask guard, for a seller acting on a list that has gone stale.

        The previous look's rows go with it: keeping decided rows would filter out everything the
        seller declined months ago, and adopted ones are remembered by the item carrying the URL.
        """
        now = _now()
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM discovered_listings WHERE market = ? AND status != 'adopted'",
                (market,),
            )
            conn.execute(
                "INSERT INTO market_surveys (market, state, requested_ts) VALUES (?, 'due', ?) "
                "ON CONFLICT (market) DO UPDATE SET state = 'due', requested_ts = excluded"
                ".requested_ts, attempts = 0, surveyed_ts = NULL",
                (market, now),
            )

    def record_survey_result(
        self,
        market: str,
        listings: list,
        *,
        notice_text: str | None = None,
        controls: list | None = None,
        ref: str | None = None,
    ) -> int:
        """Record what a survey found, close the survey, and queue the ask — one transaction.

        Returns how many listings were newly recorded; rows already present are left as they are.
        The notice rides in here because discovering and asking are one event — two transactions
        leave a window where the survey is closed, the one-ask guard spent, and the seller was never
        asked, which no later tick repairs.
        """
        now = _now()
        recorded = 0
        with self._db.transaction() as conn:
            for entry in listings:
                cur = conn.execute(
                    "INSERT INTO discovered_listings "
                    "(market, listing_id, url, title, price, price_text, status, discovered_ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?) "
                    "ON CONFLICT (market, listing_id) DO NOTHING",
                    (
                        market,
                        entry["listing_id"],
                        entry["url"],
                        entry["title"],
                        entry["price"],
                        entry.get("price_text") or "",
                        now,
                    ),
                )
                recorded += cur.rowcount
            conn.execute(
                "INSERT INTO market_surveys (market, state, requested_ts, surveyed_ts, found) "
                "VALUES (?, 'done', ?, ?, ?) ON CONFLICT (market) DO UPDATE SET "
                "state = 'done', surveyed_ts = excluded.surveyed_ts, found = excluded.found",
                (market, now, now, recorded),
            )
            if notice_text:
                _insert_notice(conn, notice_text, controls=controls, ref=ref)
        return recorded

    # --- the listings -------------------------------------------------------------------------

    def list_discovered_listings(
        self, market: str | None = None, status: str | None = None
    ) -> list[DiscoveredListing]:
        """Discovered listings, oldest first. Both filters are optional so one accessor serves the
        lane, the tools and the catchup count."""
        sql = "SELECT * FROM discovered_listings"
        clauses, params = [], []
        if market is not None:
            clauses.append("market = ?")
            params.append(market)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY discovered_ts ASC, listing_id ASC"
        return [_listing_from_row(r) for r in self._db.query(sql, tuple(params))]

    def count_pending_discovered(self) -> int:
        """How many listings are waiting on the seller's answer — what catchup surfaces so an ask
        that scrolled out of the chat is still reachable."""
        rows = self._db.query(
            "SELECT COUNT(*) AS n FROM discovered_listings WHERE status = 'pending'"
        )
        return rows[0]["n"]

    def decide_discovered_listings(
        self,
        market: str,
        *,
        decision: str,
        manage: str | None = None,
        listing_ids: list | None = None,
    ) -> int:
        """Apply the seller's answer to a market's listings; returns how many rows moved.

        The count is the answer, not a courtesy: a tap on an old button may find nothing left to
        move, and that lets the caller say so. A decline reaches acceptances that have not been
        adopted yet — changing your mind is ordinary, and reporting otherwise would claim the work
        stopped when it did not. A yes reaches only pending rows, because re-deciding an adoption
        already under way would double-queue it.

        `retry` re-arms a carousell.ai publish that failed, so it applies to adopted rows, not
        pending ones.
        """
        if decision == "retry":
            return self._retry_rail_publish(market, listing_ids)
        if decision not in ("manage", "decline"):
            raise StoreError("decision must be manage, decline or retry")
        if decision == "manage" and manage not in MANAGE_MODES:
            raise StoreError(f"manage must be one of {MANAGE_MODES}")
        status = LISTING_ACCEPTED if decision == "manage" else LISTING_DECLINED
        reachable = "('pending')" if decision == "manage" else "('pending', 'accepted')"
        sql = (
            "UPDATE discovered_listings SET status = ?, manage = ?, decided_ts = ? "
            f"WHERE market = ? AND status IN {reachable}"
        )
        params: list = [status, manage if decision == "manage" else None, _now(), market]
        if listing_ids:
            sql += f" AND listing_id IN ({', '.join('?' * len(listing_ids))})"
            params.extend(listing_ids)
        with self._db.transaction() as conn:
            return conn.execute(sql, tuple(params)).rowcount

    def _retry_rail_publish(self, market: str, listing_ids: list | None) -> int:
        """Re-arm the carousell.ai publish for adopted listings whose attempts ran out."""
        sql = (
            "UPDATE discovered_listings SET rail_state = 'owed', rail_attempts = 0, "
            "rail_pass_id = NULL WHERE market = ? AND status = 'adopted' AND rail_state = 'failed'"
        )
        params: list = [market]
        if listing_ids:
            sql += f" AND listing_id IN ({', '.join('?' * len(listing_ids))})"
            params.extend(listing_ids)
        with self._db.transaction() as conn:
            return conn.execute(sql, tuple(params)).rowcount

    def expire_stale_decisions(self, ttl_sec: float, now: float | None = None) -> int:
        """Retire asks the seller never answered; returns how many expired.

        A yes tapped against a months-old list would relist whatever has sold since. The tap on an
        expired list is not lost — it reopens the survey.
        """
        cutoff = (_now() if now is None else now) - ttl_sec
        with self._db.transaction() as conn:
            return conn.execute(
                "UPDATE discovered_listings SET status = 'expired' "
                "WHERE status = 'pending' AND discovered_ts < ?",
                (cutoff,),
            ).rowcount

    def next_adoptable_listing(self) -> DiscoveredListing | None:
        """The next accepted listing to work on, or None.

        Ordered by attempts before age, so a listing that keeps failing sits behind every untried
        one. Deliberately not filtered by attempt count: a row that has used up its attempts is
        still returned — last — so the caller can retire it. Filtering here would strand a row whose
        last attempt committed before its retirement did: stuck, holding up the batch summary, with
        nothing left that could reach it.
        """
        rows = self._db.query(
            "SELECT * FROM discovered_listings WHERE status = 'accepted' "
            "ORDER BY attempts ASC, discovered_ts ASC, listing_id ASC LIMIT 1"
        )
        return _listing_from_row(rows[0]) if rows else None

    def fail_discovered_listing(self, market: str, listing_id: str, reason: str) -> None:
        """Give up on a listing for good, with the reason on the row. Terminal."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE discovered_listings SET status = 'failed', last_error = ?, decided_ts = ? "
                "WHERE market = ? AND listing_id = ?",
                (reason[:200], _now(), market, listing_id),
            )

    def bump_listing_attempt(self, market: str, listing_id: str, reason: str) -> int:
        """Count one failed adoption and record why; returns the new attempt count."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE discovered_listings SET attempts = attempts + 1, last_error = ? "
                "WHERE market = ? AND listing_id = ?",
                (reason[:200], market, listing_id),
            )
            row = conn.execute(
                "SELECT attempts FROM discovered_listings WHERE market = ? AND listing_id = ?",
                (market, listing_id),
            ).fetchone()
        return row["attempts"] if row else 0

    # --- adoption -----------------------------------------------------------------------------

    def adopt_discovered_listing(
        self,
        market: str,
        listing_id: str,
        *,
        item_id: str | None = None,
        title: str = "",
        list_price: float | None = None,
        currency: str | None = None,
        description: str = "",
        condition: str | None = None,
        photos: list | None = None,
        url: str = "",
        rail_owed: bool = False,
    ) -> ItemRecord:
        """Turn one discovered listing into an item, in a single transaction.

        The item is inserted **carrying the marketplace URL it was read from**, and the row is
        advanced in the same transaction — that is the whole reason this is one call.

        Pass `item_id` to link rather than insert. Two callers do: a retry after a crash, where the
        item already records this URL, and a listing that turns out to be something the seller
        already has on another marketplace, where it does not. So the URL is recorded on the linked
        item when it is missing, in the same transaction — without it the item would be the twin's
        listing only, and the read lane could never join this market's buyers to it, which is the
        whole point of merging them.

        `rail_owed` marks that a carousell.ai publish is still owed; it is written here because
        nothing downstream could derive it — an item with no rail URL is indistinguishable from one
        the seller only ever wanted answered.

        Raises `ListingGone` — rolling the whole thing back — if the row is no longer accepted by
        the time the write lands: adopting takes a page read and photos, and the seller can decline
        or re-ask in that window.
        """
        if item_id is None:
            if not title or not title.strip():
                raise StoreError("an adopted listing needs a title")
            if not isinstance(list_price, (int, float)) or isinstance(list_price, bool):
                raise StoreError("an adopted listing needs a numeric price")
            if not url:
                raise StoreError("an adopted listing needs the marketplace URL it was read from")
        # Outside the transaction: it raises, and Database.transaction is not reentrant.
        stored_photos = validate_photos(photos or [])
        now = _now()
        with self._db.transaction() as conn:
            if item_id is None:
                item_id = _insert_item_in_txn(
                    conn,
                    title=title,
                    list_price=list_price,
                    currency=currency,
                    description=description,
                    condition=condition,
                    photos=stored_photos,
                    now=now,
                    # Already live somewhere the seller put it, so it was never a draft.
                    status="ready",
                    listing_urls={market: url},
                )
            elif url:
                # Linking. Merge this market's URL in rather than replacing the map: the item is
                # keeping its other marketplaces, and this is the one write that gives a merged item
                # its second listing. Left alone when the URL is already there, so the retry path
                # stays a no-op.
                stored = conn.execute(
                    "SELECT listing_urls FROM items WHERE id = ?", (item_id,)
                ).fetchone()
                if stored is None:
                    raise StoreError(f"item {item_id!r} vanished during adoption")
                urls = json.loads(stored["listing_urls"])
                if urls.get(market) != url:
                    urls[market] = url
                    conn.execute(
                        "UPDATE items SET listing_urls = ?, updated_ts = ? WHERE id = ?",
                        (json.dumps(urls, sort_keys=True), now, item_id),
                    )
            moved = conn.execute(
                "UPDATE discovered_listings SET status = 'adopted', item_id = ?, adopted_ts = ?, "
                "rail_state = ?, last_error = NULL "
                "WHERE market = ? AND listing_id = ? AND status = 'accepted'",
                (item_id, now, RAIL_OWED if rail_owed else None, market, listing_id),
            ).rowcount
            if not moved:
                # Declined, re-asked or deleted while this was being read. Raising rolls back the
                # item insert — an item with no row behind it could never be published.
                raise ListingGone(f"{listing_id!r} on {market} is no longer waiting to be adopted")
            # A new item can turn "none of ours" into a match, and the read lane caches those
            # answers. Cleared in the same transaction: this is the one funnel every adoption
            # goes through, and a cache outliving the fact it was about is how a buyer goes
            # unanswered.
            _forget_thread_listings_in_txn(conn, market)
        rows = self._db.query("SELECT * FROM items WHERE id = ?", (item_id,))
        if not rows:
            raise StoreError(f"item {item_id!r} vanished during adoption")
        return _item_from_row(rows[0])

    # --- the carousell.ai publish an adopted listing is owed ------------------------------------

    def listings_owed_rail_publish(self) -> list[DiscoveredListing]:
        """Adopted listings still owed a carousell.ai publish, oldest first."""
        rows = self._db.query(
            "SELECT * FROM discovered_listings WHERE rail_state = 'owed' "
            "ORDER BY adopted_ts ASC, listing_id ASC"
        )
        return [_listing_from_row(r) for r in rows]

    def listings_awaiting_rail_publish(self) -> list[DiscoveredListing]:
        """Adopted listings whose carousell.ai publish is in flight — what the lane watches."""
        rows = self._db.query(
            "SELECT * FROM discovered_listings WHERE rail_state = 'queued' "
            "ORDER BY adopted_ts ASC, listing_id ASC"
        )
        return [_listing_from_row(r) for r in rows]

    def set_rail_publish_queued(self, market: str, listing_id: str, pass_id: str) -> None:
        """Record that this listing's carousell.ai publish is in flight, and count the attempt."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE discovered_listings SET rail_state = 'queued', rail_pass_id = ?, "
                "rail_attempts = rail_attempts + 1 WHERE market = ? AND listing_id = ?",
                (pass_id, market, listing_id),
            )

    def set_rail_publish_state(
        self, market: str, listing_id: str, state: str, *, notice_text: str | None = None
    ) -> None:
        """Settle a carousell.ai publish — landed, owed another go, or given up on — and tell the
        seller in the same transaction when there is something to tell them.

        The notice belongs in here for the reason it does at discovery: the state change is what
        makes the notice owed, so a crash between them would lose the message or send it twice.
        """
        if state not in (RAIL_OWED, RAIL_DONE, RAIL_FAILED):
            raise StoreError(f"unknown rail publish state: {state!r}")
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE discovered_listings SET rail_state = ?, rail_pass_id = NULL "
                "WHERE market = ? AND listing_id = ?",
                (state, market, listing_id),
            )
            if notice_text:
                _insert_notice(conn, notice_text)
