"""The browser layer's handoff rows: sign-in requests from chat, and holds on the shared tab.

A handoff between two threads, not a history. The provider's receive loop writes a row when the
seller taps **Sign in on desktop** (it must not drive Chrome itself — that loop answers every other
message), and `browser.connect`'s lane reads it, serves it, and deletes it. The durable record of
what happened is the notice queued back to the seller, never a row left behind here.
"""

from __future__ import annotations

from typing import TypedDict

from sellee.db import Database
from sellee.store.helpers import UNPLACEABLE_REPORTED_META, _insert_notice, _now

# How long an answered ring is kept, as the record of when the marketplace last rang.
_RING_KEPT_SEC = 30 * 86400.0

# What the lane should do for a request. `open` is the seller asking to be signed in: navigate,
# pull the tab forward, and raise the window. `probe` is them saying they already have — re-read
# the login state without touching what is in front of them.
CONNECT_MODE_OPEN = "open"
CONNECT_MODE_PROBE = "probe"
CONNECT_MODES = (CONNECT_MODE_OPEN, CONNECT_MODE_PROBE)

# Who may claim the one shared tab, named here so the daemon and the CLIs that release a hold
# spell the same string. Two holds, released independently: a single sign-in, and an installer's
# whole marketplace phase, which outlives every sign-in inside it.
HOLD_SIGNIN = "signin"
HOLD_SETUP = "setup"

# How long a claim survives unrenewed — for a seller who wandered off or closed the terminal.
# Long enough to find a password, short enough that a dead CLI is not a permanent outage.
BROWSER_HOLD_TTL_SEC = 900.0


class MarketConnectRequest(TypedDict):
    market: str
    mode: str
    requested_ts: float


class BrowserMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

    def request_market_connect(self, market: str, mode: str = CONNECT_MODE_OPEN) -> None:
        """Ask the connect lane to sign the seller in to `market`.

        Idempotent per market by the row's primary key: a seller who taps the button twice (or
        taps Check again while an open is still pending) replaces the request they already have
        rather than queueing a second navigation of the daemon's one shared tab. The newest tap
        wins, including its mode — it is the one that reflects what they are looking at now.
        """
        if mode not in CONNECT_MODES:
            raise ValueError(f"unknown market connect mode: {mode!r}")
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO market_connect_requests (market, mode, requested_ts) "
                "VALUES (?, ?, ?) ON CONFLICT (market) DO UPDATE SET "
                "mode = excluded.mode, requested_ts = excluded.requested_ts",
                (market, mode, _now()),
            )

    def pending_market_connects(self) -> list[MarketConnectRequest]:
        """Every outstanding request, oldest first — the order the lane serves them in."""
        rows = self._db.query(
            "SELECT market, mode, requested_ts FROM market_connect_requests "
            "ORDER BY requested_ts ASC, market ASC"
        )
        return [
            MarketConnectRequest(market=r["market"], mode=r["mode"], requested_ts=r["requested_ts"])
            for r in rows
        ]

    def clear_market_connect_request(self, market: str) -> None:
        """Drop a request once it has an answer. Safe to call for a row that is already gone."""
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM market_connect_requests WHERE market = ?", (market,))

    # --- holds on the one shared tab ---------------------------------------------------------

    def hold_browser(self, holder: str, reason: str, ttl_sec: float, now: float | None = None):
        """Claim the browser for something the daemon is not driving, until `ttl_sec` from now.

        Re-claiming under the same holder renews rather than stacking: the installer takes one
        hold across a whole marketplace phase and renews it per sign-in, and a second row per
        market would leave the last one outliving the phase by a full TTL.
        """
        now = _now() if now is None else now
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO browser_holds (holder, reason, claimed_ts, expires_ts) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (holder) DO UPDATE SET "
                "reason = excluded.reason, expires_ts = excluded.expires_ts",
                (holder, reason, now, now + ttl_sec),
            )

    def release_browser_hold(self, holder: str) -> None:
        """Give the tab back. Safe for a holder that never held it, or whose hold has expired."""
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM browser_holds WHERE holder = ?", (holder,))

    # --- a marketplace that has told us to stop ------------------------------------------------

    def block_market(
        self,
        market: str,
        cause: str,
        *,
        ttl_sec: float | None,
        now: float | None = None,
    ) -> None:
        """Stop driving `market` until it is cleared. `ttl_sec` None means indefinite.

        Re-blocking the same market under the same cause renews the window and counts a strike; a
        *different* cause is a new incident, so `incident_ts` moves and the seller is owed a fresh
        telling. Strikes are what let a caller lengthen the window rather than repeat it.
        """
        now = _now() if now is None else now
        expires = None if ttl_sec is None else now + ttl_sec
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO market_blocks "
                "(market, cause, strikes, blocked_ts, incident_ts, expires_ts, told_ts) "
                "VALUES (?, ?, 1, ?, ?, ?, NULL) ON CONFLICT (market) DO UPDATE SET "
                "cause = excluded.cause, "
                "strikes = market_blocks.strikes + 1, "
                "blocked_ts = excluded.blocked_ts, "
                "expires_ts = excluded.expires_ts, "
                # A new incident re-arms the telling; the same one going on does not.
                "incident_ts = CASE WHEN market_blocks.cause = excluded.cause "
                "THEN market_blocks.incident_ts ELSE excluded.incident_ts END, "
                "told_ts = CASE WHEN market_blocks.cause = excluded.cause "
                "THEN market_blocks.told_ts ELSE NULL END",
                (market, cause, now, now, expires),
            )

    def market_block(self, market: str, now: float | None = None) -> dict | None:
        """This market's live block, or None when it is free to drive.

        An expired row reads as None rather than being deleted: a read that writes would turn every
        lane tick into a transaction, and the row costs nothing until the next block overwrites it.
        A NULL `expires_ts` never expires — see the migration for why that case exists.
        """
        now = _now() if now is None else now
        rows = self._db.query(
            "SELECT market, cause, strikes, blocked_ts, incident_ts, expires_ts, told_ts "
            "FROM market_blocks WHERE market = ? AND (expires_ts IS NULL OR expires_ts > ?)",
            (market, now),
        )
        return dict(rows[0]) if rows else None

    def blocked_markets(self, now: float | None = None) -> list[str]:
        """Every market currently blocked. For the gates that ask about a set rather than one."""
        now = _now() if now is None else now
        rows = self._db.query(
            "SELECT market FROM market_blocks WHERE expires_ts IS NULL OR expires_ts > ? "
            "ORDER BY market ASC",
            (now,),
        )
        return [str(row["market"]) for row in rows]

    def market_block_strikes(self, market: str) -> int:
        """How many times this market has hit a wall with no clean probe in between.

        Read regardless of expiry, unlike `market_block`. The row outlives its own window — only a
        confirmed-clean probe deletes it — so a wall that comes back after a block lapsed is a
        repeat rather than a first offence, which is what makes the escalating window mean what its
        comment says.
        """
        rows = self._db.query("SELECT strikes FROM market_blocks WHERE market = ?", (market,))
        return int(rows[0]["strikes"]) if rows else 0

    def clear_market_block(self, market: str) -> None:
        """Let this market be driven again. Only ever called once something has proved it is
        clear — never by a read that merely happened to succeed."""
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM market_blocks WHERE market = ?", (market,))

    def report_market_block_once(self, market: str, text: str, controls: list | None = None):
        """Tell the seller this market is blocked — once per incident, not once per strike.

        Returns whether this queued the notice. One transaction, because saying it and recording
        that we said it are one event: split in two, a crash between them tells the seller twice.

        The guard is `told_ts` against the incident, so a block that expired and re-triggered is a
        new thing to say while the same one going on is not.
        """
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE market_blocks SET told_ts = ? WHERE market = ? AND told_ts IS NULL",
                (_now(), market),
            )
            if not cur.rowcount:
                return False
            _insert_notice(conn, text, controls=controls)
            return True

    # --- a marketplace ringing --------------------------------------------------------------

    def record_rings(
        self,
        market: str,
        rings,
        *,
        due_ts: float,
        now: float | None = None,
        answered: bool = False,
    ) -> int:
        """Record the rings `(ring_key, kind, shown_ts)` not heard before. Returns how many were.

        `answered` records them as already dealt with — what the doorbell hears the first time it
        listens can be days old, and is evidence the doorbell works, not a reason to visit. Answered
        rings older than a month are let go in the same transaction.
        """
        now = _now() if now is None else now
        handled = now if answered else None
        added = 0
        with self._db.transaction() as conn:
            for ring_key, kind, shown_ts in rings:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO market_rings "
                    "(market, ring_key, kind, shown_ts, heard_ts, due_ts, handled_ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (market, str(ring_key), kind, float(shown_ts), now, due_ts, handled),
                )
                added += cur.rowcount
            conn.execute(
                "DELETE FROM market_rings WHERE handled_ts IS NOT NULL AND heard_ts < ?",
                (now - _RING_KEPT_SEC,),
            )
        return added

    def ring_owed(self, market: str, now: float | None = None) -> bool:
        """Whether a message ring on this market is due a visit that has not happened."""
        now = _now() if now is None else now
        rows = self._db.query(
            "SELECT 1 FROM market_rings WHERE market = ? AND kind = 'message' "
            "AND handled_ts IS NULL AND due_ts <= ? LIMIT 1",
            (market, now),
        )
        return bool(rows)

    def answer_rings(self, market: str, *, heard_before: float, now: float | None = None) -> int:
        """Mark answered every ring heard before the visit began. One heard while it was under way
        was not read by it, and stays owed."""
        now = _now() if now is None else now
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE market_rings SET handled_ts = ? "
                "WHERE market = ? AND handled_ts IS NULL AND heard_ts <= ?",
                (now, market, heard_before),
            )
            return cur.rowcount

    def last_ring_ts(self, market: str) -> float | None:
        """When this market last rang with anything at all, or None if it never has."""
        rows = self._db.query(
            "SELECT MAX(shown_ts) AS shown FROM market_rings WHERE market = ?", (market,)
        )
        shown = rows[0]["shown"] if rows else None
        return float(shown) if shown is not None else None

    def ring_count(self, market: str) -> int:
        rows = self._db.query("SELECT COUNT(*) AS n FROM market_rings WHERE market = ?", (market,))
        return int(rows[0]["n"]) if rows else 0

    # --- what a conversation is about -------------------------------------------------------

    def record_thread_listing(
        self, thread_id: str, market: str, product_id: str, row_key: str, now: float | None = None
    ) -> None:
        """Remember which listing a conversation is about — including that it is none of ours.

        The empty `product_id` is the case worth having: a conversation about a listing we do not
        manage is re-asked on every sweep otherwise, and for a market that names the listing only
        inside the conversation, asking costs a page load each time.
        """
        now = _now() if now is None else now
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO thread_listing_lookups "
                "(thread_id, market, product_id, row_key, looked_ts) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (thread_id) DO UPDATE SET product_id = excluded.product_id, "
                "row_key = excluded.row_key, looked_ts = excluded.looked_ts",
                (thread_id, market, product_id, row_key, now),
            )

    def thread_listing_lookup(self, thread_id: str) -> dict | None:
        """What we last learned about this conversation, or None if we have never looked."""
        rows = self._db.query(
            "SELECT product_id, row_key FROM thread_listing_lookups WHERE thread_id = ?",
            (thread_id,),
        )
        if not rows:
            return None
        return {"product_id": rows[0]["product_id"], "row_key": rows[0]["row_key"]}

    def clear_thread_listings(self, market: str) -> int:
        """Forget this market's lookups, positives and negatives, so the next sweep asks again.

        Wholesale on purpose: forgetting too much costs a page load, forgetting too little costs
        a buyer nobody answers.
        """
        with self._db.transaction() as conn:
            return conn.execute(
                "DELETE FROM thread_listing_lookups WHERE market = ?", (market,)
            ).rowcount

    def report_unplaceable_once(self, market: str, text: str) -> bool:
        """Tell the seller buyers are waiting in conversations we cannot place — once per market.

        Returns whether this queued the notice. The guard is the `meta` row's primary key, the same
        way `request_market_survey` lets the key decide: two lanes racing, or a lane that keeps
        finding the same buyers every sweep, cannot spend the ask twice.

        Once, not once per set: keying on which conversations were unplaceable meant a tenth buyer
        arriving re-sent the whole notice, and on 2026-09-04 nine became twenty-one in six minutes
        and the seller was told twice about something they had already decided. The count is not
        what makes it worth a message; the fact is, and the fact does not change.

        One transaction, because saying it and recording that we said it are one event — split in
        two, a crash between them sends the notice a second time on the next sweep.
        """
        with self._db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING",
                (UNPLACEABLE_REPORTED_META.format(market=market), str(_now())),
            )
            if not cur.rowcount:
                return False
            _insert_notice(conn, text)
            return True

    def browser_hold_reason(self, now: float | None = None) -> str:
        """Why the browser is spoken for, or "" when it is free.

        Expired rows are ignored rather than deleted on read: a read that writes turns every lane
        tick into a transaction, and the row costs nothing until the next claim overwrites it.
        """
        now = _now() if now is None else now
        rows = self._db.query(
            "SELECT reason FROM browser_holds WHERE expires_ts > ? "
            "ORDER BY expires_ts DESC LIMIT 1",
            (now,),
        )
        return str(rows[0]["reason"]) if rows else ""

    # --- the seller's Craigslist account -------------------------------------------------------
    # Each write names the state it moves from; a notice is queued in the move's own transaction.

    def craigslist_account(self) -> dict | None:
        rows = self._db.query("SELECT * FROM craigslist_account WHERE id = 1")
        return dict(rows[0]) if rows else None

    def request_craigslist_signup(self) -> bool:
        """Ask for a Craigslist account; False when there already is one, or one on its way."""
        now = _now()
        with self._db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO craigslist_account (id, state, requested_ts, updated_ts) "
                "VALUES (1, 'signup_requested', ?, ?) ON CONFLICT (id) DO NOTHING",
                (now, now),
            )
            return bool(cur.rowcount)

    def set_craigslist_awaiting_activation(self) -> bool:
        """Keeps any link already recorded: the activation mail can beat this write."""
        now = _now()
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE craigslist_account SET state = 'awaiting_activation', "
                "requested_ts = ?, late_reported = 0, updated_ts = ? "
                "WHERE id = 1 AND state = 'signup_requested'",
                (now, now),
            )
            return bool(cur.rowcount)

    def record_craigslist_link(self, link: str, *, states: tuple) -> bool:
        """Keep an emailed link for the lane to open, if the account is in one of `states`."""
        marks = ", ".join("?" for _ in states)
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE craigslist_account SET link = ?, link_opened = 0, updated_ts = ? "
                f"WHERE id = 1 AND state IN ({marks}) AND link IS NOT ?",
                (link, _now(), *states, link),
            )
            return bool(cur.rowcount)

    def mark_craigslist_link_opened(self, link: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE craigslist_account SET link_opened = 1, updated_ts = ? "
                "WHERE id = 1 AND link = ?",
                (_now(), link),
            )

    def request_craigslist_login(self) -> bool:
        """An active account found signed out: wait for a login link, none sent yet."""
        now = _now()
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE craigslist_account SET state = 'awaiting_login_link', link = NULL, "
                "link_opened = 0, requested_ts = 0, late_reported = 0, updated_ts = ? "
                "WHERE id = 1 AND state = 'active'",
                (now,),
            )
            return bool(cur.rowcount)

    def mark_craigslist_login_requested(self, asked_ts: float) -> None:
        """A login link was asked for at `asked_ts` (the lane's clock); it voids any earlier one."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE craigslist_account SET link = NULL, link_opened = 0, requested_ts = ?, "
                "updated_ts = ? WHERE id = 1 AND state = 'awaiting_login_link'",
                (asked_ts, _now()),
            )

    def reset_craigslist_login(self) -> None:
        """A login link that did not sign in is spent: ask for a new one."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE craigslist_account SET link = NULL, link_opened = 0, requested_ts = 0, "
                "updated_ts = ? WHERE id = 1 AND state = 'awaiting_login_link'",
                (_now(),),
            )

    def record_craigslist_login_link(self, link: str, *, received_ts: float) -> bool:
        """Keep a login link sent no earlier than the request now awaited; an older one is void."""
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE craigslist_account SET link = ?, link_opened = 0, updated_ts = ? "
                "WHERE id = 1 AND state = 'awaiting_login_link' AND requested_ts > 0 "
                "AND requested_ts <= ? AND link IS NOT ?",
                (link, _now(), received_ts, link),
            )
            return bool(cur.rowcount)

    def restore_craigslist_session(self) -> bool:
        """The account is signed in again."""
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE craigslist_account SET state = 'active', updated_ts = ? "
                "WHERE id = 1 AND state = 'awaiting_login_link'",
                (_now(),),
            )
            return bool(cur.rowcount)

    def activate_craigslist_account(self, notice: str) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE craigslist_account SET state = 'active', updated_ts = ? "
                "WHERE id = 1 AND state = 'awaiting_activation'",
                (_now(),),
            )
            if cur.rowcount:
                _insert_notice(conn, notice)
            return bool(cur.rowcount)

    def abandon_craigslist_account(self, notice: str) -> bool:
        """Drop an account Craigslist never finished, so connecting again starts over."""
        with self._db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM craigslist_account "
                "WHERE id = 1 AND state IN ('signup_requested', 'awaiting_activation')"
            )
            if cur.rowcount:
                _insert_notice(conn, notice)
            return bool(cur.rowcount)

    def report_craigslist_late_once(self, notice: str) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE craigslist_account SET late_reported = 1, updated_ts = ? "
                "WHERE id = 1 AND state IN ('awaiting_activation', 'awaiting_login_link') "
                "AND late_reported = 0",
                (_now(),),
            )
            if cur.rowcount:
                _insert_notice(conn, notice)
            return bool(cur.rowcount)
