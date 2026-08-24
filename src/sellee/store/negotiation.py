"""Both negotiation ledgers and the checkout gate — one money path."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sellee.db import Database
from sellee.engines import buyer_negotiate as buyer_engine
from sellee.engines import negotiate as negotiate_engine
from sellee.store import (
    BudgetRecord,
    CheckoutRecord,
    ItemNotFound,
    StoreError,
    WantNotFound,
    _now,
)


class NegotiationMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

    if TYPE_CHECKING:
        # Owned by WantsMixin, called from the buy-side ledger below. Declared and never defined:
        # only the composed Store has both mixins, and a checker looking at this class alone
        # cannot know that. The real body is the one that runs.
        def get_budget(self, want_id: str) -> BudgetRecord | None: ...

    # --- sell-side negotiation --------------------------------------------------------------

    def _load_negotiation(self, conn, item_id: str) -> dict:
        row = conn.execute("SELECT * FROM negotiations WHERE item_id = ?", (item_id,)).fetchone()
        if row:
            front_runner = json.loads(row["front_runner"]) if row["front_runner"] else None
            state, sold_to, is_bidding = row["state"], row["sold_to"], bool(row["is_bidding"])
        else:
            front_runner, state, sold_to, is_bidding = None, "open", None, False
        buyers = {}
        for b in conn.execute(
            "SELECT * FROM negotiation_buyers WHERE item_id = ?", (item_id,)
        ).fetchall():
            buyers[b["thread_id"]] = {
                "buyer_handle": b["buyer_handle"],
                "offers": json.loads(b["offers"]),
                "highest_offer": b["highest_offer"],
                "rounds_used": b["rounds_used"],
                "last_counter": b["last_counter"],
                "lowball_count": b["lowball_count"],
                "status": b["status"],
            }
        return {
            "state": state,
            "is_bidding": is_bidding,
            "front_runner": front_runner,
            "sold_to": sold_to,
            "buyers": buyers,
        }

    def _persist_negotiation(self, conn, item_id: str, led: dict) -> None:
        conn.execute(
            "INSERT INTO negotiations "
            "(item_id, state, is_bidding, front_runner, sold_to, updated_ts) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (item_id) DO UPDATE SET "
            "state = excluded.state, is_bidding = excluded.is_bidding, "
            "front_runner = excluded.front_runner, sold_to = excluded.sold_to, "
            "updated_ts = excluded.updated_ts",
            (
                item_id,
                led["state"],
                1 if led["is_bidding"] else 0,
                json.dumps(led["front_runner"], sort_keys=True) if led["front_runner"] else None,
                led["sold_to"],
                _now(),
            ),
        )
        for thread_id, b in led["buyers"].items():
            conn.execute(
                "INSERT INTO negotiation_buyers "
                "(item_id, thread_id, buyer_handle, offers, highest_offer, rounds_used, "
                " last_counter, lowball_count, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (item_id, thread_id) DO UPDATE SET "
                "buyer_handle = excluded.buyer_handle, offers = excluded.offers, "
                "highest_offer = excluded.highest_offer, rounds_used = excluded.rounds_used, "
                "last_counter = excluded.last_counter, lowball_count = excluded.lowball_count, "
                "status = excluded.status",
                (
                    item_id,
                    thread_id,
                    b["buyer_handle"],
                    json.dumps(b["offers"]),
                    b["highest_offer"],
                    b["rounds_used"],
                    b["last_counter"],
                    b["lowball_count"],
                    b["status"],
                ),
            )

    def _item_for_negotiation(self, conn, item_id: str) -> tuple:
        item = conn.execute(
            "SELECT list_price, currency FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if not item:
            raise ItemNotFound(f"no item with id {item_id!r}")
        return item["list_price"], item["currency"]

    def negotiate_offer(
        self,
        item_id: str,
        thread_id: str,
        handle: str,
        offer: float,
        *,
        config,
        firmness: str | None = None,
    ) -> dict:
        """One offer, decided and persisted in a single transaction — the serialization that makes
        FCFS single-inventory hold. Floorless orchestration lives here (not the engine): at/above
        list persists the default floor, below list holds for the seller's floor ask, no list price
        is a data error; the engine is then handed a complete floor."""
        with self._db.transaction() as conn:
            list_price, currency = self._item_for_negotiation(conn, item_id)
            floor_row = conn.execute(
                "SELECT * FROM floors WHERE item_id = ?", (item_id,)
            ).fetchone()

            if floor_row is None:
                if not isinstance(list_price, (int, float)) or list_price <= 0:
                    raise StoreError(f"item {item_id!r} has no valid list price to negotiate")
                if offer < list_price:
                    return self._hold_for_floor(conn, item_id, thread_id, handle, offer, currency)
                # at/above list needs no real floor: persist the documented default (= list price)
                conn.execute(
                    "INSERT INTO floors (item_id, floor, currency, source, updated_ts) "
                    "VALUES (?, ?, ?, 'default', ?)",
                    (item_id, list_price, currency, _now()),
                )
                floor_row = conn.execute(
                    "SELECT * FROM floors WHERE item_id = ?", (item_id,)
                ).fetchone()

            floor = floor_row["floor"]
            floor_record = {
                "auto_counter_step": floor_row["auto_counter_step"],
                "auto_counter_rounds": floor_row["auto_counter_rounds"],
            }
            knobs = negotiate_engine.resolve_knobs(config, floor_record, firmness)

            led = self._load_negotiation(conn, item_id)
            buyer = led["buyers"].get(thread_id) or negotiate_engine.blank_buyer(handle)
            buyer["buyer_handle"] = handle
            negotiate_engine.record_offer(buyer, offer)

            res = negotiate_engine.decide(
                offer, thread_id, buyer, led, floor, list_price, knobs["step"], knobs
            )
            self._apply_offer_transition(led, thread_id, buyer, res)
            led["buyers"][thread_id] = buyer
            self._persist_negotiation(conn, item_id, led)
            res["item_state"] = led["state"]
            res["currency"] = currency
            return res

    def _hold_for_floor(self, conn, item_id, thread_id, handle, offer, currency) -> dict:
        """No floor and a below-list offer: record the offer and hold, so the caller asks the
        seller for the floor once. Nothing is decided (no rounds/front-runner consumed), and the
        held offer still bars rivals via other_best once the floor lands."""
        led = self._load_negotiation(conn, item_id)
        if led["state"] == "sold":
            return {
                "decision": "sold",
                "needs_seller_confirm": False,
                "message_intent": "item_sold",
                "item_state": "sold",
                "currency": currency,
            }
        buyer = led["buyers"].get(thread_id) or negotiate_engine.blank_buyer(handle)
        buyer["buyer_handle"] = handle
        negotiate_engine.record_offer(buyer, offer, held_for_floor=True)
        led["buyers"][thread_id] = buyer
        self._persist_negotiation(conn, item_id, led)
        return {
            "decision": "needs_floor",
            "counter_price": None,
            "hold_firm": False,
            "needs_seller_confirm": True,
            "message_intent": "hold_for_floor",
            "item_state": led["state"],
            "currency": currency,
        }

    @staticmethod
    def _apply_offer_transition(led, thread_id, buyer, res) -> None:
        decision = res["decision"]
        if decision == "counter":
            buyer["rounds_used"] += 1
            buyer["last_counter"] = res["counter_price"]
        elif decision == "deflect_lowball":
            buyer["lowball_count"] += 1
        elif decision == "hold_firm" and res["message_intent"] == "disengage":
            buyer["status"] = "passed"
        elif decision == "accept_fcfs":
            buyer["status"] = "front_runner"
            led["front_runner"] = {
                "thread_id": thread_id,
                "amount": res["accept_price"],
                "kind": "fcfs",
            }
            led["state"] = "reserved_provisional"
        elif decision == "bid_lead":
            buyer["status"] = "leading_bid"
            led["is_bidding"] = True
            led["state"] = "bidding"
            led["front_runner"] = {
                "thread_id": thread_id,
                "amount": res["leading_amount"],
                "kind": "bid",
            }
        elif decision == "bid_outbid":
            led["is_bidding"] = True
            led["state"] = "bidding"

    def negotiate_status(self, item_id: str) -> dict:
        with self._db.transaction() as conn:
            self._item_for_negotiation(conn, item_id)
            led = self._load_negotiation(conn, item_id)
        return {
            "item_state": led["state"],
            "is_bidding": led["is_bidding"],
            "front_runner": led["front_runner"],
            "buyers": {
                t: {"status": b["status"], "highest_offer": b["highest_offer"]}
                for t, b in led["buyers"].items()
            },
        }

    def negotiate_confirm_bid(self, item_id: str, thread_id: str) -> dict:
        with self._db.transaction() as conn:
            self._item_for_negotiation(conn, item_id)
            led = self._load_negotiation(conn, item_id)
            fr = led["front_runner"]
            if not fr or fr.get("thread_id") != thread_id:
                raise StoreError("thread is not the current leading bid")
            led["state"] = "reserved_provisional"
            led["buyers"][thread_id]["status"] = "won"
            for tid, b in led["buyers"].items():
                if tid != thread_id and b.get("status") != "passed":
                    b["status"] = "outbid"
            self._persist_negotiation(conn, item_id, led)
            return {
                "reserved_for": thread_id,
                "amount": fr["amount"],
                "item_state": led["state"],
                "tell_others": "outbid",
            }

    def negotiate_confirm_sold(self, item_id: str, thread_id: str) -> dict:
        with self._db.transaction() as conn:
            row = conn.execute("SELECT listing_urls FROM items WHERE id = ?", (item_id,)).fetchone()
            if not row:
                raise ItemNotFound(f"no item with id {item_id!r}")
            led = self._load_negotiation(conn, item_id)
            led["state"] = "sold"
            led["sold_to"] = thread_id
            urls = json.loads(row["listing_urls"])
            won_platform = thread_id.split(":", 1)[0] if ":" in thread_id else None
            take_down = [
                {"platform": p, "url": u} for p, u in urls.items() if u and p != won_platform
            ]
            close = [
                t
                for t, b in led["buyers"].items()
                if t != thread_id and b.get("status") not in ("lost", "passed")
            ]
            for t in close:
                led["buyers"][t]["status"] = "lost"
            self._persist_negotiation(conn, item_id, led)
            return {"item_state": "sold", "take_down": take_down, "close_threads": close}

    def negotiate_release(self, item_id: str) -> dict:
        with self._db.transaction() as conn:
            self._item_for_negotiation(conn, item_id)
            led = self._load_negotiation(conn, item_id)
            led["state"] = "bidding" if led["is_bidding"] else "open"
            led["front_runner"] = None
            for b in led["buyers"].values():
                if b.get("status") in ("front_runner", "won"):
                    b["status"] = "active"
            self._persist_negotiation(conn, item_id, led)
            return {"item_state": led["state"]}

    # --- buy-side negotiation ---------------------------------------------------------------

    def _budget_for_engine(self, want_id: str) -> dict:
        rec = self.get_budget(want_id)
        if rec is None:
            raise StoreError(f"no budget set for want {want_id!r} — set one before negotiating")
        return {
            "target": rec["target_price"],
            "max_budget": rec["max_budget"],
            "currency": rec["currency"] or "",
            "step": rec["auto_counter_step"] or buyer_engine.DEFAULT_STEP,
            "max_rounds": rec["auto_counter_rounds"] or buyer_engine.DEFAULT_MAX_ROUNDS,
            "opening_ratio": rec["opening_ratio"] or buyer_engine.DEFAULT_OPENING_RATIO,
        }

    def _load_buyer_negotiation(self, conn, want_id: str) -> dict:
        row = conn.execute(
            "SELECT * FROM buyer_negotiations WHERE want_id = ?", (want_id,)
        ).fetchone()
        state = row["state"] if row else "shopping"
        committed_thread = row["committed_thread"] if row else None
        sellers = {}
        for s in conn.execute(
            "SELECT * FROM buyer_negotiation_sellers WHERE want_id = ?", (want_id,)
        ).fetchall():
            sellers[s["thread_id"]] = {
                "seller_handle": s["seller_handle"],
                "listed_price": s["listed_price"],
                "our_offers": json.loads(s["our_offers"]),
                "our_highest_offer": s["our_highest_offer"],
                "seller_lowest_ask": s["seller_lowest_ask"],
                "rounds_used": s["rounds_used"],
                "last_offer": s["last_offer"],
                "agreed_price": s["agreed_price"],
                "status": s["status"],
            }
        return {"state": state, "committed_thread": committed_thread, "sellers": sellers}

    def _persist_buyer_negotiation(self, conn, want_id: str, led: dict) -> None:
        conn.execute(
            "INSERT INTO buyer_negotiations (want_id, state, committed_thread, updated_ts) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (want_id) DO UPDATE SET "
            "state = excluded.state, committed_thread = excluded.committed_thread, "
            "updated_ts = excluded.updated_ts",
            (want_id, led["state"], led["committed_thread"], _now()),
        )
        for thread_id, s in led["sellers"].items():
            conn.execute(
                "INSERT INTO buyer_negotiation_sellers "
                "(want_id, thread_id, seller_handle, listed_price, our_offers, our_highest_offer, "
                " seller_lowest_ask, rounds_used, last_offer, agreed_price, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (want_id, thread_id) DO UPDATE SET "
                "seller_handle = excluded.seller_handle, listed_price = excluded.listed_price, "
                "our_offers = excluded.our_offers, "
                "our_highest_offer = excluded.our_highest_offer, "
                "seller_lowest_ask = excluded.seller_lowest_ask, "
                "rounds_used = excluded.rounds_used, "
                "last_offer = excluded.last_offer, agreed_price = excluded.agreed_price, "
                "status = excluded.status",
                (
                    want_id,
                    thread_id,
                    s["seller_handle"],
                    s["listed_price"],
                    json.dumps(s["our_offers"]),
                    s["our_highest_offer"],
                    s["seller_lowest_ask"],
                    s["rounds_used"],
                    s["last_offer"],
                    s["agreed_price"],
                    s["status"],
                ),
            )

    def _require_want(self, conn, want_id: str) -> None:
        if not conn.execute("SELECT 1 FROM wants WHERE want_id = ?", (want_id,)).fetchone():
            raise WantNotFound(f"no want with id {want_id!r}")

    def buyer_negotiate_seed(
        self,
        want_id: str,
        thread_id: str,
        seller_handle: str,
        *,
        listed_price=None,
        our_last=None,
        seller_ask=None,
        rounds=None,
    ) -> dict:
        """Seed a seller entry for a thread the user started by hand, without emitting an offer or
        reading the budget — records the user's prior offer so the next reply climbs from it."""
        with self._db.transaction() as conn:
            self._require_want(conn, want_id)
            led = self._load_buyer_negotiation(conn, want_id)
            seller = led["sellers"].get(thread_id) or buyer_engine.blank_seller(
                seller_handle, listed_price
            )
            if seller_handle:
                seller["seller_handle"] = seller_handle
            if listed_price is not None:
                seller["listed_price"] = listed_price
            if seller_ask is not None:
                seller["seller_lowest_ask"] = (
                    seller_ask
                    if seller["seller_lowest_ask"] is None
                    else min(seller["seller_lowest_ask"], seller_ask)
                )
            if our_last is not None:
                amt = int(our_last)
                seller["our_offers"].append({"amount": amt, "source": "user_prior"})
                seller["our_highest_offer"] = max(seller["our_highest_offer"], amt)
                seller["last_offer"] = amt
            if rounds is not None:
                seller["rounds_used"] = max(seller["rounds_used"], int(rounds))
            seller["status"] = "negotiating"
            led["sellers"][thread_id] = seller
            self._persist_buyer_negotiation(conn, want_id, led)
            return {
                "decision": "seeded",
                "thread": thread_id,
                "our_last": seller["last_offer"],
                "seller_lowest_ask": seller["seller_lowest_ask"],
                "rounds_used": seller["rounds_used"],
                "want_state": led["state"],
            }

    def buyer_negotiate_open(
        self, want_id: str, thread_id: str, seller_handle: str, listed: float, ask=None
    ) -> dict:
        budget = self._budget_for_engine(want_id)
        with self._db.transaction() as conn:
            self._require_want(conn, want_id)
            led = self._load_buyer_negotiation(conn, want_id)
            seller = led["sellers"].get(thread_id) or buyer_engine.blank_seller(
                seller_handle, listed
            )
            seller["seller_handle"] = seller_handle
            seller["listed_price"] = listed
            if ask is not None:
                seller["seller_lowest_ask"] = (
                    ask
                    if seller["seller_lowest_ask"] is None
                    else min(seller["seller_lowest_ask"], ask)
                )
            res = buyer_engine.guard(
                buyer_engine.decide_open(
                    listed, budget["target"], budget["max_budget"], budget["opening_ratio"]
                ),
                budget["max_budget"],
            )
            if res["decision"] == "opening_offer":
                amt = res["offer_price"]
                seller["our_offers"].append({"amount": amt})
                seller["our_highest_offer"] = max(seller["our_highest_offer"], amt)
                seller["last_offer"] = amt
            elif res["decision"] == "accept":
                seller["status"] = "deal_pending"
                seller["agreed_price"] = res["accept_price"]
            led["sellers"][thread_id] = seller
            self._persist_buyer_negotiation(conn, want_id, led)
            res["want_state"] = led["state"]
            res["currency"] = budget["currency"]
            return res

    def buyer_negotiate_reply(self, want_id: str, thread_id: str, seller_price: float) -> dict:
        budget = self._budget_for_engine(want_id)
        with self._db.transaction() as conn:
            self._require_want(conn, want_id)
            led = self._load_buyer_negotiation(conn, want_id)
            seller = led["sellers"].get(thread_id) or buyer_engine.blank_seller("", seller_price)
            seller["seller_lowest_ask"] = (
                seller_price
                if seller["seller_lowest_ask"] is None
                else min(seller["seller_lowest_ask"], seller_price)
            )
            res = buyer_engine.guard(
                buyer_engine.decide_reply(
                    seller_price,
                    seller,
                    led,
                    thread_id,
                    budget["target"],
                    budget["max_budget"],
                    budget["step"],
                    budget["max_rounds"],
                ),
                budget["max_budget"],
            )
            decision = res["decision"]
            if decision == "counter":
                amt = res["offer_price"]
                seller["our_offers"].append({"amount": amt})
                seller["our_highest_offer"] = max(seller["our_highest_offer"], amt)
                seller["last_offer"] = amt
                seller["rounds_used"] += 1
            elif decision == "accept":
                seller["status"] = "deal_pending"
                seller["agreed_price"] = res["accept_price"]
            elif decision == "walk_away":
                seller["status"] = "walked"
            led["sellers"][thread_id] = seller
            self._persist_buyer_negotiation(conn, want_id, led)
            res["want_state"] = led["state"]
            res["currency"] = budget["currency"]
            return res

    def buyer_negotiate_accept(self, want_id: str, thread_id: str) -> dict:
        budget = self._budget_for_engine(want_id)
        with self._db.transaction() as conn:
            self._require_want(conn, want_id)
            led = self._load_buyer_negotiation(conn, want_id)
            seller = led["sellers"].get(thread_id)
            if not seller:
                raise StoreError("no such thread on this want")
            led["state"] = "committed"
            led["committed_thread"] = thread_id
            seller["status"] = "committed"
            close = [
                t
                for t, s in led["sellers"].items()
                if t != thread_id and s.get("status") not in ("walked", "lost", "unavailable")
            ]
            for t in close:
                led["sellers"][t]["status"] = "lost"
            self._persist_buyer_negotiation(conn, want_id, led)
            return {
                "committed_thread": thread_id,
                "deal_price": seller.get("agreed_price") or seller.get("last_offer"),
                "close_threads": close,
                "want_state": "committed",
                "currency": budget["currency"],
            }

    def buyer_negotiate_walk(self, want_id: str, thread_id: str) -> dict:
        with self._db.transaction() as conn:
            self._require_want(conn, want_id)
            led = self._load_buyer_negotiation(conn, want_id)
            seller = led["sellers"].get(thread_id)
            if not seller:
                raise StoreError("no such thread on this want")
            seller["status"] = "walked"
            self._persist_buyer_negotiation(conn, want_id, led)
            return {"thread": thread_id, "want_state": led["state"]}

    # --- checkout ---------------------------------------------------------------------------

    def checkout_floor_gate(self, item_id: str, price: float) -> dict:
        """The floor gate for a checkout close, returning only a status — never the floor value.
        Floorless orchestration lives here: at/above list persists the documented default floor;
        below list returns no_floor (ask the seller); below the floor returns below_floor."""
        with self._db.transaction() as conn:
            item = conn.execute(
                "SELECT list_price, currency FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if not item:
                raise ItemNotFound(f"no item with id {item_id!r}")
            list_price, currency = item["list_price"], item["currency"]
            floor_row = conn.execute(
                "SELECT floor FROM floors WHERE item_id = ?", (item_id,)
            ).fetchone()
            if floor_row is None:
                if not isinstance(list_price, (int, float)) or list_price <= 0:
                    raise StoreError(f"item {item_id!r} has no valid list price")
                if price < list_price:
                    return {"status": "no_floor", "currency": currency}
                conn.execute(
                    "INSERT INTO floors (item_id, floor, currency, source, updated_ts) "
                    "VALUES (?, ?, ?, 'default', ?)",
                    (item_id, list_price, currency, _now()),
                )
                floor = list_price
            else:
                floor = floor_row["floor"]
            if price < floor:
                return {"status": "below_floor", "currency": currency}
            return {"status": "ok", "currency": currency}

    def get_checkout(self, sale_id: str) -> CheckoutRecord | None:
        rows = self._db.query("SELECT * FROM checkouts WHERE sale_id = ?", (sale_id,))
        if not rows:
            return None
        row = rows[0]
        return {
            "sale_id": row["sale_id"],
            "item_id": row["item_id"],
            "thread_id": row["thread_id"],
            "checkout_url": row["checkout_url"],
            "price": row["price"],
            "currency": row["currency"],
            "issued_ts": row["issued_ts"],
        }

    def record_checkout(
        self, *, sale_id: str, item_id: str, thread_id: str, checkout_url: str, price, currency
    ) -> CheckoutRecord:
        """Persist the checkout record idempotently — the deterministic sale_id maps to one record;
        a re-record returns the existing one, never a second link."""
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO checkouts "
                "(sale_id, item_id, thread_id, checkout_url, price, currency, issued_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sale_id, item_id, thread_id, checkout_url, price, currency, _now()),
            )
        return self.get_checkout(sale_id)  # type: ignore[return-value]
