"""The browser layer's handoff rows: sign-in requests from chat, and holds on the shared tab.

A handoff between two threads, not a history. The provider's receive loop writes a row when the
seller taps **Sign in on desktop** (it must not drive Chrome itself — that loop answers every other
message), and `browser.connect`'s lane reads it, serves it, and deletes it. The durable record of
what happened is the notice queued back to the seller, never a row left behind here.
"""

from __future__ import annotations

from typing import TypedDict

from sellee.db import Database
from sellee.store.helpers import _now

# What the lane should do for a request. `open` is the seller asking to be signed in: navigate,
# pull the tab forward, and raise the window. `probe` is them saying they already have — re-read
# the login state without touching what is in front of them.
CONNECT_MODE_OPEN = "open"
CONNECT_MODE_PROBE = "probe"
CONNECT_MODES = (CONNECT_MODE_OPEN, CONNECT_MODE_PROBE)

# Who may claim the one shared tab, named here so the daemon and the CLIs that release a hold are
# spelling the same string. Two of them, released independently: a single sign-in, and an
# installer's whole marketplace phase — which outlives every sign-in inside it, and is what keeps
# the lanes out of the gap between one market finishing and the next one opening.
HOLD_SIGNIN = "signin"
HOLD_SETUP = "setup"

# How long a claim survives unrenewed. The claimant is a CLI blocked on a person, so this is not
# how long a sign-in takes — it is how long the agent stays out of the way for a seller who
# wandered off, closed the terminal, or Ctrl-C'd. Long enough to find a password, short enough that
# a dead CLI is not a permanent outage.
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
        """Forget this market's lookups, so the next sweep asks again. Returns how many went.

        Deliberately wholesale — positives as well as negatives. Narrower would mean working out
        which conversations a newly adopted listing could possibly have changed the answer for,
        and the failure direction here is safe either way: the cost of forgetting too much is a
        page load, where the cost of forgetting too little is a buyer nobody answers.
        """
        with self._db.transaction() as conn:
            return conn.execute(
                "DELETE FROM thread_listing_lookups WHERE market = ?", (market,)
            ).rowcount

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
