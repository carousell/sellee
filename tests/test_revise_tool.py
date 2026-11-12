"""update_live_listing and record_listing_revision: the whole edit in one call, one honest
verdict per marketplace, and the refusals that keep an edit from moving a deal under a buyer."""

from __future__ import annotations

import threading
import time

import pytest
from tests.conftest import seed_setting
from tests.fake_carousell_ai_mcp import FakeRelay, serve

import sellee.tools  # noqa: F401  registration
from sellee import paths
from sellee.channel import activity
from sellee.config import Config
from sellee.rail import inbox as relay
from sellee.rail.client import RailClient, RailToolError
from sellee.rail.sink import RelayReplySink
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_EDIT,
    ToolError,
    all_specs,
)
from sellee.tools.registry import dispatch as _dispatch

RAIL_URL = "https://www.carousell.ai/listing/L1"
FB_URL = "https://www.facebook.com/marketplace/item/123/"
CAROUSELL_URL = "https://www.carousell.sg/p/dyson-v8-456/"


class FakeRail:
    def __init__(self, *, fail=None):
        self.fail = fail
        self.updates: list = []
        self.uploads = 0

    def update_listing(self, listing_id, **fields):
        if self.fail:
            raise RailToolError(self.fail)
        self.updates.append((listing_id, fields))
        return {}

    def upload_photo(self, data, content_type):
        self.uploads += 1
        return f"enc-{self.uploads}"


def _listed(store, *, markets=("carousell-ai", "fb", "carousell"), **kw) -> dict:
    base = {"title": "Dyson V8", "list_price": 150.0, "currency": "SGD", "description": "Works."}
    base.update(kw)
    item = store.create_item(**base)
    urls = {"carousell-ai": RAIL_URL, "fb": FB_URL, "carousell": CAROUSELL_URL}
    for market in markets:
        store.record_listing_url(item["id"], market, urls[market])
    return item


def _edit(ctx, item_id, **fields):
    return _dispatch("update_live_listing", {"item_id": item_id, "fields": fields}, ctx)


@pytest.fixture
def rail():
    return FakeRail()


@pytest.fixture
def ctx(make_ctx, rail):
    return make_ctx(TIER_PASS_CHANNEL, pass_id="p1", rail_factory=lambda: rail)


# --- the fan-out --------------------------------------------------------------------------------


def test_a_price_drop_lands_on_the_rail_and_queues_each_browser_market(ctx, store, rail) -> None:
    item = _listed(store)
    result = _edit(ctx, item["id"], list_price=120)

    assert result["changed"] == ["list_price"]
    assert result["markets"]["carousell-ai"] == {"status": "updated"}
    assert result["markets"]["fb"]["status"] == "queued"
    assert result["markets"]["carousell"]["status"] == "queued"
    # money converted in code, and only the field that changed travels
    assert rail.updates == [("L1", {"price_cents": 12000})]
    assert store.get_item(item["id"])["list_price"] == 120
    pending = store.list_listing_revisions("pending")
    assert sorted(r["market"] for r in pending) == ["carousell", "fb"]


def test_a_description_edit_sends_only_the_description(ctx, store, rail) -> None:
    item = _listed(store, markets=("carousell-ai",))
    result = _edit(ctx, item["id"], description="Comes with the original box.")
    assert rail.updates == [("L1", {"description": "Comes with the original box."})]
    assert result["markets"] == {"carousell-ai": {"status": "updated"}}


def test_an_item_not_on_the_rail_is_only_queued(ctx, store, rail) -> None:
    item = _listed(store, markets=("fb",))
    result = _edit(ctx, item["id"], title="Dyson V8 Absolute")
    assert rail.updates == []
    assert set(result["markets"]) == {"fb"}


# --- honesty about what did not happen ----------------------------------------------------------


def test_a_rail_refusal_is_reported_in_its_own_words_and_the_rest_still_goes(
    make_ctx, store
) -> None:
    rail = FakeRail(fail="price_cents must be positive")
    ctx = make_ctx(TIER_PASS_CHANNEL, rail_factory=lambda: rail)
    item = _listed(store)
    result = _edit(ctx, item["id"], list_price=120)
    assert result["markets"]["carousell-ai"] == {
        "status": "failed",
        "reason": "price_cents must be positive",
    }
    assert result["markets"]["fb"]["status"] == "queued"


def test_no_rail_in_the_session_is_a_failure_not_a_success(make_ctx, store) -> None:
    ctx = make_ctx(TIER_PASS_CHANNEL)
    item = _listed(store, markets=("carousell-ai",))
    result = _edit(ctx, item["id"], list_price=120)
    assert result["markets"]["carousell-ai"]["status"] == "failed"


def test_a_disconnected_marketplace_is_named_with_its_link_and_not_queued(ctx, store) -> None:
    seed_setting(store, "connected_markets", ["carousell"])
    item = _listed(store)
    result = _edit(ctx, item["id"], list_price=120)
    assert result["markets"]["fb"] == {"status": "not_connected", "url": FB_URL}
    assert [r["market"] for r in store.list_listing_revisions("pending")] == ["carousell"]


def test_a_change_the_driver_cannot_make_is_manual_not_half_done(ctx, store, rail) -> None:
    media = paths.media_dir()
    media.mkdir(parents=True, exist_ok=True)
    photos = [{"path": str(media / "a.jpg"), "uploaded_url": "enc-a"}]
    item = _listed(store, photos=photos)
    result = _edit(ctx, item["id"], photos=[str(media / "a.jpg")])
    # Facebook's driver does not replace photos; Carousell's recipe can.
    assert result["markets"]["fb"] == {"status": "manual", "url": FB_URL}
    assert result["markets"]["carousell"]["status"] == "queued"


# --- refusals -----------------------------------------------------------------------------------


def test_a_price_change_while_a_deal_is_in_flight_is_refused(ctx, store, rail) -> None:
    item = _listed(store)
    store.set_floor(item["id"], 100.0, "seller")
    store.negotiate_offer(item["id"], "fb:1", "buyer", 200, config=Config())  # above list: bidding
    with pytest.raises(ToolError, match="negotiate_release"):
        _edit(ctx, item["id"], list_price=120)
    assert store.get_item(item["id"])["list_price"] == 150.0
    assert rail.updates == []


def test_words_can_still_change_while_a_deal_is_in_flight(ctx, store, rail) -> None:
    item = _listed(store, markets=("carousell-ai",))
    store.negotiate_offer(item["id"], "fb:1", "buyer", 200, config=Config())
    result = _edit(ctx, item["id"], description="Also has a spare filter.")
    assert result["markets"]["carousell-ai"] == {"status": "updated"}


def test_a_sold_item_is_refused(ctx, store, monkeypatch) -> None:
    item = _listed(store)
    monkeypatch.setattr(store, "sold_item_ids", lambda: {item["id"]})
    with pytest.raises(ToolError, match="sold"):
        _edit(ctx, item["id"], list_price=120)


def test_a_paused_agent_is_refused(ctx, store, monkeypatch) -> None:
    item = _listed(store)
    monkeypatch.setattr(store, "is_paused", lambda: True)
    with pytest.raises(ToolError, match="paused"):
        _edit(ctx, item["id"], list_price=120)


@pytest.mark.parametrize("fields", [{"currency": "USD"}, {"condition": "like new"}, {}])
def test_fields_a_live_listing_cannot_take_are_refused(ctx, store, fields) -> None:
    item = _listed(store)
    with pytest.raises(ToolError):
        _dispatch("update_live_listing", {"item_id": item["id"], "fields": fields}, ctx)


def test_a_bad_price_is_refused_before_anything_moves(ctx, store, rail) -> None:
    item = _listed(store)
    with pytest.raises(ToolError):
        _edit(ctx, item["id"], list_price=-5)
    assert rail.updates == []
    assert store.list_listing_revisions() == []


def test_the_update_tool_is_on_the_channel_tier_only_among_passes(make_ctx, store) -> None:
    item = _listed(store)
    with pytest.raises(Exception, match="unknown tool"):
        _edit(make_ctx(TIER_PASS_EDIT), item["id"], list_price=120)


# --- the floor and the offers -------------------------------------------------------------------


def test_a_drop_under_the_floor_lowers_it_and_says_so_without_a_number(ctx, store) -> None:
    item = _listed(store)
    store.set_floor(item["id"], 140.0, "seller")
    result = _edit(ctx, item["id"], list_price=120)
    assert result["floor_lowered"] is True
    assert store.get_floor(item["id"])["floor"] == 120.0
    assert "140" not in repr(result) and 'floor": 1' not in repr(result)


def test_standing_offers_above_the_new_price_are_counted(ctx, store) -> None:
    item = _listed(store)
    store.set_floor(item["id"], 100.0, "seller")
    store.negotiate_offer(item["id"], "fb:1", "a", 130, config=Config())
    store.negotiate_offer(item["id"], "fb:2", "b", 110, config=Config())
    result = _edit(ctx, item["id"], list_price=120)
    assert result["offers_above_new_price"] == 1


# --- photos -------------------------------------------------------------------------------------


def test_reordered_photos_keep_their_uploads_and_replace_the_rail_set(ctx, store, rail) -> None:
    media = paths.media_dir()
    media.mkdir(parents=True, exist_ok=True)
    a, b = str(media / "a.jpg"), str(media / "b.jpg")
    item = _listed(
        store,
        markets=("carousell-ai",),
        photos=[{"path": a, "uploaded_url": "enc-a"}, {"path": b, "uploaded_url": "enc-b"}],
    )
    _edit(ctx, item["id"], photos=[b, a])
    assert rail.uploads == 0  # nothing new, so nothing re-uploaded
    ((_, fields),) = rail.updates
    assert fields["media"] == {"urls": [{"url": "enc-b", "type": 1}, {"url": "enc-a", "type": 1}]}


# --- record_listing_revision --------------------------------------------------------------------


def _running_edit_pass(store, item_id) -> tuple:
    rev = store.queue_listing_revision(item_id, "carousell", ["list_price"])
    store.claim_listing_revision()
    store.attach_revision_pass(rev, "pass_edit_1")
    return rev


def test_an_edit_pass_records_its_own_outcome(make_ctx, store) -> None:
    item = _listed(store)
    rev = _running_edit_pass(store, item["id"])
    ctx = make_ctx(TIER_PASS_EDIT, pass_id="pass_edit_1")
    result = _dispatch(
        "record_listing_revision",
        {"revision_id": rev, "outcome": "done", "shown": {"price": "S$120"}},
        ctx,
    )
    assert result == {"revision_id": rev, "status": "done"}
    row = store.get_listing_revision(rev)
    assert row["status"] == "done" and row["accepted"] == {"price": "S$120"}


def test_an_edit_pass_cannot_settle_another_pass_s_edit(make_ctx, store) -> None:
    item = _listed(store)
    rev = _running_edit_pass(store, item["id"])
    ctx = make_ctx(TIER_PASS_EDIT, pass_id="pass_somebody_else")
    with pytest.raises(ToolError, match="another pass"):
        _dispatch("record_listing_revision", {"revision_id": rev, "outcome": "done"}, ctx)
    assert store.get_listing_revision(rev)["status"] == "running"


def test_recording_an_edit_that_is_not_running_is_refused(make_ctx, store) -> None:
    item = _listed(store)
    rev = store.queue_listing_revision(item["id"], "carousell", ["list_price"])  # still pending
    with pytest.raises(ToolError, match="no edit in progress"):
        _dispatch(
            "record_listing_revision",
            {"revision_id": rev, "outcome": "failed"},
            make_ctx(TIER_ATTENDED),
        )


def test_a_queueing_failure_leaves_the_rail_untouched(ctx, store, rail, monkeypatch) -> None:
    # Local bookkeeping fails before anything public happens — never after the rail was edited.
    item = _listed(store)

    def broken(*args, **kwargs):
        raise RuntimeError("no such table: listing_revisions")

    monkeypatch.setattr(store, "queue_listing_revision", broken)
    with pytest.raises(ToolError):
        _edit(ctx, item["id"], list_price=125)
    assert rail.updates == []


# --- past enquirers hear about a carousell.ai price drop -----------------------------------------

_WEB = "https://www.carousell.ai"
_FAST = Config(reply_delay_sec=(0, 0), interactive_reply_delay_sec=(0, 0))
_DROP = "The price of the Teak lamp is now $80 (was $95)."


@pytest.fixture
def relay_fake():
    state = FakeRelay()
    state._clock = time.time() - 1000  # bazaar's clock: these threads are older than any edit
    state.listings.append({"title": "Teak lamp"})  # bazaar's listing L1
    base, shutdown = serve(state)
    state.base = base
    yield state
    shutdown()


def _relay_client(fake):
    return RailClient(api_base=fake.base, api_key="k", web_base_url=_WEB, timeout_sec=5.0)


def _read_relay(store, bus, fake, *, later_sec: float = 0.0) -> None:
    deps = relay.RelayDeps(
        store=store,
        bus=bus,
        config=Config(),
        rail_factory=lambda: _relay_client(fake),
        now=lambda: time.time() + later_sec,
    )
    relay.relay_lane(deps)


def _relay_ctx(make_ctx, store, bus, fake):
    sink = RelayReplySink(
        client=_relay_client(fake), store=store, bus=bus, retry_delays_sec=(0, 0, 0)
    )
    return make_ctx(
        TIER_PASS_CHANNEL,
        pass_id="p1",
        rail_factory=lambda: _relay_client(fake),
        reply_sink=sink,
        config=_FAST,
    )


def _asked(fake, thread_id: str, *, answered: bool = True) -> None:
    fake.add_thread(thread_id, listing_id="L1")
    fake.add_message(thread_id, f"{thread_id}-q", "buyer", "Is the lamp still available?")
    if answered:
        fake.add_message(thread_id, f"{thread_id}-a", "agent", "Yes, it is.")


def _lamp(store) -> dict:
    item = store.create_item(title="Teak lamp", list_price=95.0, currency="SGD")
    store.record_listing_url(item["id"], "carousell-ai", f"{_WEB}/listing/L1")
    return item


def _updates_sent(fake) -> list:
    return sorted((call["id"], call["text"]) for call in fake.reply_calls)


def _seller_hears(store) -> list:
    """Every notice queued for the seller, then the activity update their untold tells make."""
    update = activity.untold_text(store)
    return [n["text"] for n in store.list_queued_notices()] + ([update] if update else [])


def test_a_rail_price_drop_tells_each_buyer_who_asked_once(make_ctx, store, bus, relay_fake):
    item = _lamp(store)
    for thread_id in ("t1", "t2", "passed", "blocked"):
        _asked(relay_fake, thread_id)
    _asked(relay_fake, "waiting", answered=False)  # owed an answer, which will carry the price
    relay_fake.block("blocked")
    _read_relay(store, bus, relay_fake)
    with store._db.transaction() as conn:  # noqa: SLF001 — a buyer who already passed
        conn.execute(
            "INSERT INTO negotiations (item_id, state, updated_ts) VALUES (?, 'open', 0)",
            (item["id"],),
        )
        conn.execute(
            "INSERT INTO negotiation_buyers (item_id, thread_id, status) VALUES (?, ?, 'passed')",
            (item["id"], "carousell-ai:passed"),
        )

    result = _edit(_relay_ctx(make_ctx, store, bus, relay_fake), item["id"], list_price=80)

    assert result["markets"]["carousell-ai"] == {"status": "updated"}
    assert result["buyers_told"] == 2
    assert _updates_sent(relay_fake) == [("t1", _DROP), ("t2", _DROP)]
    one = {"buyers": 1, "price_drop": True, "new_photos": False, "item_title": "Teak lamp"}
    assert [t["facts"] for t in store.untold_tells()] == [one, one]
    assert _seller_hears(store) == [
        "📣 Teak lamp — let 2 buyers who asked know about the price drop."
    ]
    for thread_id in ("carousell-ai:t1", "carousell-ai:t2"):
        thread = store.get_thread(thread_id)
        assert thread["last_followup_ts"] is not None
        assert [m["text"] for m in thread["messages"] if m["dir"] == "out"][-1] == _DROP
    # The waiting buyer is still owed their answer.
    assert "carousell-ai:waiting" in {
        row["thread_id"] for row in store.threads_with_unhandled_inbound()
    }


def test_a_thread_that_was_told_hears_nothing_more_until_the_buyer_writes(
    make_ctx, store, bus, relay_fake
):
    item = _lamp(store)
    _asked(relay_fake, "t1")
    _read_relay(store, bus, relay_fake)
    ctx = _relay_ctx(make_ctx, store, bus, relay_fake)
    _edit(ctx, item["id"], list_price=80)
    _read_relay(store, bus, relay_fake)  # bazaar shows our update stored

    _edit(ctx, item["id"], list_price=70)
    assert len(relay_fake.reply_calls) == 1

    relay_fake._clock = time.time() + 5  # the buyer writes after the update
    relay_fake.add_message("t1", "t1-q2", "buyer", "Would you take 60?")
    relay_fake.add_message("t1", "t1-a2", "agent", "Sorry, no.")
    _read_relay(store, bus, relay_fake)
    _edit(ctx, item["id"], list_price=65)
    assert [c["text"] for c in relay_fake.reply_calls][-1] == (
        "The price of the Teak lamp is now $65 (was $70)."
    )
    assert len(relay_fake.reply_calls) == 2


def test_a_nudge_since_the_buyer_wrote_does_not_hold_back_a_price_drop(
    make_ctx, store, bus, relay_fake
):
    item = _lamp(store)
    _asked(relay_fake, "t1")
    _read_relay(store, bus, relay_fake)
    ctx = _relay_ctx(make_ctx, store, bus, relay_fake)
    nudge = {"thread_id": "carousell-ai:t1", "text": "Still keen?", "kind": "followup"}
    assert _dispatch("send_reply", nudge, ctx)["status"] == "sent"

    result = _edit(ctx, item["id"], list_price=80)

    assert result["buyers_told"] == 1
    assert [c["text"] for c in relay_fake.reply_calls] == ["Still keen?", _DROP]


def test_a_rail_price_rise_tells_nobody(make_ctx, store, bus, relay_fake):
    item = _lamp(store)
    _asked(relay_fake, "t1")
    _read_relay(store, bus, relay_fake)
    _edit(_relay_ctx(make_ctx, store, bus, relay_fake), item["id"], list_price=120)
    assert relay_fake.reply_calls == []
    assert _seller_hears(store) == []


def test_a_failed_rail_push_tells_nobody(make_ctx, store, bus, relay_fake):
    item = _lamp(store)
    _asked(relay_fake, "t1")
    _read_relay(store, bus, relay_fake)
    relay_fake.listings.clear()  # bazaar no longer has the listing, so the update fails
    result = _edit(_relay_ctx(make_ctx, store, bus, relay_fake), item["id"], list_price=80)
    assert result["markets"]["carousell-ai"]["status"] == "failed"
    assert relay_fake.reply_calls == []
    assert _seller_hears(store) == []


class _BuyerWritesMidSend:
    """A sink during whose send the relay lane imports a new buyer message."""

    def __init__(self, store):
        self.store = store

    def send(self, thread, text, kind, intent_id):
        self.store.append_thread_message(
            thread["thread_id"], msg_id="late", direction="in", text="Still there?"
        )
        return {}


def test_a_buyer_message_that_lands_during_the_update_is_still_owed_an_answer(
    make_ctx, store, rail
):
    item = _listed(store, markets=("carousell-ai",))
    store.create_thread(
        thread_id="carousell-ai:t1",
        side="sell",
        market="carousell-ai",
        counterpart_handle="b",
        item_id=item["id"],
    )
    store.append_thread_message("carousell-ai:t1", msg_id="q", direction="in", text="hi", ts=1.0)
    store.mark_relay_answered("carousell-ai:t1", "q", 1.0)
    ctx = make_ctx(
        TIER_PASS_CHANNEL,
        rail_factory=lambda: rail,
        reply_sink=_BuyerWritesMidSend(store),
        config=_FAST,
    )

    _edit(ctx, item["id"], list_price=120)

    assert store.get_thread("carousell-ai:t1")["cursor_last_msg_id"] == "q"
    assert "carousell-ai:t1" in {r["thread_id"] for r in store.threads_with_unhandled_inbound()}


def test_a_buyer_who_wrote_before_the_update_was_stored_is_still_owed_an_answer(
    make_ctx, store, bus, relay_fake
):
    # bazaar holds the buyer's new message before our update, but sellee has not read it yet.
    item = _lamp(store)
    _asked(relay_fake, "t1")
    _read_relay(store, bus, relay_fake)
    relay_fake._clock = time.time() + 5
    relay_fake.add_message("t1", "t1-q2", "buyer", "Can you hold it till Friday?")

    _edit(_relay_ctx(make_ctx, store, bus, relay_fake), item["id"], list_price=80)
    _read_relay(store, bus, relay_fake)

    assert "carousell-ai:t1" in {r["thread_id"] for r in store.threads_with_unhandled_inbound()}


class _LedgerMovesMidRun:
    """A sink during whose first send another buyer passes on the item."""

    def __init__(self, store, item_id, passing_thread):
        self.store, self.item_id, self.passing = store, item_id, passing_thread
        self.sent: list = []

    def send(self, thread, text, kind, intent_id):
        if not self.sent:
            with self.store._db.transaction() as conn:  # noqa: SLF001 — the negotiation lands
                conn.execute(
                    "INSERT OR IGNORE INTO negotiations (item_id, state, updated_ts) "
                    "VALUES (?, 'open', 0)",
                    (self.item_id,),
                )
                conn.execute(
                    "INSERT INTO negotiation_buyers (item_id, thread_id, status) "
                    "VALUES (?, ?, 'passed')",
                    (self.item_id, self.passing),
                )
        self.sent.append(thread["thread_id"])
        return {}


def _answered_rail_thread(store, item_id, thread_id):
    store.create_thread(
        thread_id=thread_id,
        side="sell",
        market="carousell-ai",
        counterpart_handle="b",
        item_id=item_id,
    )
    store.append_thread_message(thread_id, msg_id="q", direction="in", text="hi", ts=1.0)
    store.mark_relay_answered(thread_id, "q", 1.0)


def test_a_buyer_who_passes_while_updates_go_out_is_not_sent_one(make_ctx, store, rail):
    item = _listed(store, markets=("carousell-ai",))
    _answered_rail_thread(store, item["id"], "carousell-ai:a")
    _answered_rail_thread(store, item["id"], "carousell-ai:b")
    sink = _LedgerMovesMidRun(store, item["id"], "carousell-ai:b")
    ctx = make_ctx(TIER_PASS_CHANNEL, rail_factory=lambda: rail, reply_sink=sink, config=_FAST)

    _edit(ctx, item["id"], list_price=120)

    assert sink.sent == ["carousell-ai:a"]
    assert _seller_hears(store) == [
        "📣 Dyson V8 — let 1 buyer who asked know about the price drop."
    ]


def test_a_failing_event_write_does_not_turn_a_landed_edit_into_an_error(
    make_ctx, store, bus, rail, monkeypatch
):
    item = _listed(store, markets=("carousell-ai",))
    _answered_rail_thread(store, item["id"], "carousell-ai:a")
    sink = _LedgerMovesMidRun(store, item["id"], "carousell-ai:none")
    ctx = make_ctx(TIER_PASS_CHANNEL, rail_factory=lambda: rail, reply_sink=sink, config=_FAST)
    publish = bus.publish

    def broken(kind, payload, **kw):
        if kind.startswith("past_enquirers."):
            raise RuntimeError("database is locked")
        return publish(kind, payload, **kw)

    monkeypatch.setattr(bus, "publish", broken)
    result = _edit(ctx, item["id"], list_price=120)

    assert result["markets"]["carousell-ai"] == {"status": "updated"}
    assert _seller_hears(store) == [
        "📣 Dyson V8 — let 1 buyer who asked know about the price drop."
    ]


def test_the_seller_hears_about_the_buyers_told_before_a_send_blew_up(
    make_ctx, store, rail, monkeypatch
):
    item = _listed(store, markets=("carousell-ai",))
    _answered_rail_thread(store, item["id"], "carousell-ai:a")
    _answered_rail_thread(store, item["id"], "carousell-ai:b")
    sink = _LedgerMovesMidRun(store, item["id"], "carousell-ai:none")
    monkeypatch.setattr(
        "sellee.tools.reply._reserve_and_send",
        _raising_on("carousell-ai:b"),
    )
    ctx = make_ctx(TIER_PASS_CHANNEL, rail_factory=lambda: rail, reply_sink=sink, config=_FAST)

    result = _edit(ctx, item["id"], list_price=120)

    assert result["markets"]["carousell-ai"] == {"status": "updated"}
    assert _seller_hears(store) == [
        "📣 Dyson V8 — let 1 buyer who asked know about the price drop."
    ]


def _raising_on(thread_id):
    from sellee.tools import reply

    real = reply._reserve_and_send

    def reserve_and_send(ctx, params, thread, kind, sink, announces=None):
        if params["thread_id"] == thread_id:
            raise RuntimeError("no such table: send_intents")
        return real(ctx, params, thread, kind, sink, announces)

    return reserve_and_send


class _PriceMovesMidRun:
    """A sink during whose first send another edit raises the price from a second thread."""

    def __init__(self, store, item_id, edit_again):
        self.store, self.item_id, self.edit_again = store, item_id, edit_again
        self.seen: list = []  # (message, the live price as it went)
        self.other = None

    def send(self, thread, text, kind, intent_id):
        if self.other is None:
            self.other = threading.Thread(target=self.edit_again)
            self.other.start()
            self.other.join(timeout=0.5)  # long enough for an unserialised edit to land
        self.seen.append((text, self.store.get_item(self.item_id)["list_price"]))
        return {}


def test_an_edit_that_lands_while_updates_go_out_never_leaves_a_stale_price(make_ctx, store, rail):
    item = _listed(store, markets=("carousell-ai",))
    _answered_rail_thread(store, item["id"], "carousell-ai:a")
    _answered_rail_thread(store, item["id"], "carousell-ai:b")
    ctx = None
    sink = _PriceMovesMidRun(store, item["id"], lambda: _edit(ctx, item["id"], list_price=200))
    ctx = make_ctx(TIER_PASS_CHANNEL, rail_factory=lambda: rail, reply_sink=sink, config=_FAST)

    _edit(ctx, item["id"], list_price=120)
    sink.other.join(timeout=5)

    assert [price for _text, price in sink.seen] == [120, 120]
    assert store.get_item(item["id"])["list_price"] == 200


def _update_intents(store) -> list:
    return [
        row["status"]
        for row in store._db.query(  # noqa: SLF001 — the send bracket's durable record
            "SELECT status FROM send_intents WHERE kind = 'followup'"
        )
    ]


def test_an_update_lost_in_an_outage_is_dropped_not_sent_late(make_ctx, store, bus, relay_fake):
    item = _lamp(store)
    _asked(relay_fake, "t1")
    _read_relay(store, bus, relay_fake)
    relay_fake.reply_script = ["internal"] * 4  # every attempt fails before bazaar stores it

    _edit(_relay_ctx(make_ctx, store, bus, relay_fake), item["id"], list_price=80)
    assert _update_intents(store) == ["sent_unverified"]
    attempts = len(relay_fake.reply_calls)

    # Long after, the price could have moved or the buyer passed: the lane must not send it now.
    _read_relay(store, bus, relay_fake, later_sec=relay.RETRY_SEND_AFTER_SEC + 60)

    assert len(relay_fake.reply_calls) == attempts
    assert not any(m["text"] == _DROP for m in relay_fake.messages["t1"])
    assert _update_intents(store) == []


def test_an_update_bazaar_stored_during_an_outage_is_settled_not_dropped(
    make_ctx, store, bus, relay_fake
):
    item = _lamp(store)
    _asked(relay_fake, "t1")
    _read_relay(store, bus, relay_fake)
    relay_fake.reply_script = ["lost"] * 4  # stored on the first attempt, every answer lost

    result = _edit(_relay_ctx(make_ctx, store, bus, relay_fake), item["id"], list_price=80)
    assert result["buyers_told"] == 0 and _seller_hears(store) == []
    _read_relay(store, bus, relay_fake, later_sec=relay.RETRY_SEND_AFTER_SEC + 60)
    _read_relay(store, bus, relay_fake, later_sec=relay.RETRY_SEND_AFTER_SEC + 120)

    assert [m["text"] for m in relay_fake.messages["t1"]].count(_DROP) == 1
    assert _update_intents(store) == ["committed"]
    # Told when the lane settles it, and only once however often it reads the thread again.
    assert _seller_hears(store) == [
        "📣 Teak lamp — let 1 buyer who asked know about the price drop."
    ]


def test_a_stored_update_the_thread_read_missed_is_settled_under_bazaars_id(
    make_ctx, store, bus, relay_fake
):
    item = _lamp(store)
    _asked(relay_fake, "t1")
    _read_relay(store, bus, relay_fake)
    stamp = relay_fake.threads["t1"]["updated"]
    relay_fake.reply_script = ["lost"] * 4

    _edit(_relay_ctx(make_ctx, store, bus, relay_fake), item["id"], list_price=80)
    relay_fake.threads["t1"]["updated"] = stamp - 0.5  # behind the cursor: the read skips it
    _read_relay(store, bus, relay_fake, later_sec=relay.RETRY_SEND_AFTER_SEC + 60)

    stored = next(m for m in relay_fake.messages["t1"] if m["text"] == _DROP)
    outbound = [m for m in store.get_thread("carousell-ai:t1")["messages"] if m["dir"] == "out"]
    assert _update_intents(store) == ["committed"]
    assert (outbound[-1]["msg_id"], outbound[-1]["text"]) == (stored["id"], _DROP)
    assert [m["text"] for m in relay_fake.messages["t1"]].count(_DROP) == 1


def test_a_refused_send_is_not_counted_as_a_buyer_told(make_ctx, store, rail, monkeypatch):
    from sellee.tools import reply

    item = _listed(store, markets=("carousell-ai",))
    _answered_rail_thread(store, item["id"], "carousell-ai:a")
    _answered_rail_thread(store, item["id"], "carousell-ai:b")
    real = reply._send_reply

    def send_reply(ctx, params, **kw):
        if params["thread_id"] == "carousell-ai:b":
            raise ToolError("thread is 'held' — not eligible for a followup")
        return real(ctx, params, **kw)

    monkeypatch.setattr(reply, "_send_reply", send_reply)
    sink = _LedgerMovesMidRun(store, item["id"], "carousell-ai:none")
    ctx = make_ctx(TIER_PASS_CHANNEL, rail_factory=lambda: rail, reply_sink=sink, config=_FAST)

    result = _edit(ctx, item["id"], list_price=120)

    assert result["buyers_told"] == 1
    assert _seller_hears(store) == [
        "📣 Dyson V8 — let 1 buyer who asked know about the price drop."
    ]


def test_the_result_counts_the_buyers_told_so_nothing_is_reported_as_pending(
    make_ctx, store, bus, relay_fake
):
    """A seller was once told an update "already pending for one buyer may still go out", because
    the result carried no count. It carries the count, and the description says to report it."""
    item = _lamp(store)
    _asked(relay_fake, "t1")
    _read_relay(store, bus, relay_fake)
    ctx = _relay_ctx(make_ctx, store, bus, relay_fake)

    assert _edit(ctx, item["id"], list_price=80)["buyers_told"] == 1
    assert _edit(ctx, item["id"], list_price=70)["buyers_told"] == 0  # already told once
    assert _edit(ctx, item["id"], description="Solid teak.")["buyers_told"] == 0
    (spec,) = [s for s in all_specs() if s.name == "update_live_listing"]
    description = spec.description
    assert "buyers_told" in description and "never describe an update" in description


def test_a_buyer_a_price_drop_reopens_gets_one_message_and_it_is_not_the_update(
    make_ctx, store, bus, relay_fake
):
    """Bob offered $45 on a $50 lamp with a $48 minimum. The price drops to $40, pulling the
    minimum down with it: Bob is the negotiation's now (owed a reply), not the update's."""
    item = store.create_item(title="Teak lamp", list_price=50.0, currency="SGD")
    store.record_listing_url(item["id"], "carousell-ai", f"{_WEB}/listing/L1")
    store.set_floor(item["id"], 48.0, "seller")
    _asked(relay_fake, "bob")
    _asked(relay_fake, "cara")
    _read_relay(store, bus, relay_fake)
    store.negotiate_offer(item["id"], "carousell-ai:bob", "bob", 45, config=Config())

    result = _edit(_relay_ctx(make_ctx, store, bus, relay_fake), item["id"], list_price=40)

    assert result["floor_lowered"] is True
    assert result["buyers_told"] == 1
    assert [call["id"] for call in relay_fake.reply_calls] == ["cara"]
    owed = {
        t["thread_id"]: t["reply_owed_ts"] for t in store.item_threads(item["id"], "carousell-ai")
    }
    assert owed["carousell-ai:bob"] is not None and owed["carousell-ai:cara"] is None
    assert store.owed_offer("carousell-ai:bob") == {"offer": 45.0, "currency": "SGD"}
    # His one message is the reply he is owed, decided on his $45: now above the price, so the
    # engine takes it as a lead for the seller to confirm.
    decided = store.negotiate_offer(item["id"], "carousell-ai:bob", "bob", 45, config=Config())
    assert (decided["decision"], decided["needs_seller_confirm"]) == ("bid_lead", True)


def test_a_seller_who_turned_updates_off_tells_nobody_of_a_rail_price_drop(
    make_ctx, store, bus, relay_fake
):
    item = _lamp(store)
    _asked(relay_fake, "t1")
    _read_relay(store, bus, relay_fake)
    ctx = _relay_ctx(make_ctx, store, bus, relay_fake)
    turned = _dispatch(
        "propose_setting_change", {"key": "past_enquirer_updates", "raw_value": "off"}, ctx
    )
    assert turned["status"] == "applied"
    before = _seller_hears(store)  # the change's own Undo notice

    result = _edit(ctx, item["id"], list_price=80)

    assert result["markets"]["carousell-ai"] == {"status": "updated"}
    assert result["buyers_told"] == 0
    assert relay_fake.reply_calls == []
    assert _seller_hears(store) == before
    assert store.untold_tells() == []
    assert store.get_thread("carousell-ai:t1")["last_followup_ts"] is None


# --- past enquirers hear about new photos on carousell.ai ----------------------------------------

_PHOTOS = f"I've added new photos of the Teak lamp: {_WEB}/listing/L1."


class _MediaOnClient(RailClient):
    """The relay fake, with bazaar's media upload switched on; the upload itself is stubbed."""

    def offers_tool(self, name: str) -> bool:
        return True

    def upload_photo(self, data: bytes, content_type: str) -> str:
        return f"enc-{data[3:].decode()}"  # the photo's name, after the JPEG header


def _photo(name: str) -> str:
    media = paths.media_dir()
    media.mkdir(parents=True, exist_ok=True)
    path = media / f"{name}.jpg"
    path.write_bytes(b"\xff\xd8\xff" + name.encode())
    return str(path.resolve())


def _photo_lamp(store) -> dict:
    """The lamp, live on carousell.ai with photos a and b already uploaded."""
    item = store.create_item(
        title="Teak lamp",
        list_price=95.0,
        currency="SGD",
        photos=[{"path": _photo(n), "uploaded_url": f"enc-{n}"} for n in ("a", "b")],
    )
    store.record_listing_url(item["id"], "carousell-ai", f"{_WEB}/listing/L1")
    return item


def _media_ctx(make_ctx, store, bus, fake, client: type[RailClient] = _MediaOnClient):
    def rail():
        return client(api_base=fake.base, api_key="k", web_base_url=_WEB, timeout_sec=5.0)

    sink = RelayReplySink(client=_relay_client(fake), store=store, bus=bus, retry_delays_sec=(0,))
    return make_ctx(
        TIER_PASS_CHANNEL, pass_id="p1", rail_factory=rail, reply_sink=sink, config=_FAST
    )


def _asked_about_photo_lamp(make_ctx, store, bus, fake, client: type[RailClient] = _MediaOnClient):
    item = _photo_lamp(store)
    for thread_id in ("t1", "t2", "passed"):
        _asked(fake, thread_id)
    _read_relay(store, bus, fake)
    with store._db.transaction() as conn:  # noqa: SLF001 — a buyer who already passed
        conn.execute(
            "INSERT INTO negotiations (item_id, state, updated_ts) VALUES (?, 'open', 0)",
            (item["id"],),
        )
        conn.execute(
            "INSERT INTO negotiation_buyers (item_id, thread_id, status) VALUES (?, ?, 'passed')",
            (item["id"], "carousell-ai:passed"),
        )
    return item, _media_ctx(make_ctx, store, bus, fake, client)


def test_a_new_photo_on_the_rail_tells_each_buyer_who_asked_once(make_ctx, store, bus, relay_fake):
    item, ctx = _asked_about_photo_lamp(make_ctx, store, bus, relay_fake)

    result = _edit(ctx, item["id"], photos=[_photo("a"), _photo("b"), _photo("c")])

    assert result["markets"]["carousell-ai"] == {"status": "updated"}
    pushed = [m["url"] for m in relay_fake.listings[0]["media"]["urls"]]
    assert pushed == ["enc-a", "enc-b", "enc-c"]
    assert _updates_sent(relay_fake) == [("t1", _PHOTOS), ("t2", _PHOTOS)]
    assert _seller_hears(store) == [
        "📣 Teak lamp — let 2 buyers who asked know about the new photos."
    ]
    for thread_id in ("carousell-ai:t1", "carousell-ai:t2"):
        assert store.get_thread(thread_id)["last_followup_ts"] is not None


@pytest.mark.parametrize(
    "new_set",
    [("b", "a"), ("a",), ("b",), ("a", "b")],
    ids=["reordered", "first-removed", "second-removed", "unchanged"],
)
def test_photos_removed_or_reordered_tell_nobody(make_ctx, store, bus, relay_fake, new_set):
    item, ctx = _asked_about_photo_lamp(make_ctx, store, bus, relay_fake)

    result = _edit(ctx, item["id"], photos=[_photo(n) for n in new_set])

    assert result["markets"]["carousell-ai"] == {"status": "updated"}
    assert relay_fake.reply_calls == []
    assert _seller_hears(store) == []


def test_a_replaced_photo_counts_as_new(make_ctx, store, bus, relay_fake):
    item, ctx = _asked_about_photo_lamp(make_ctx, store, bus, relay_fake)
    _edit(ctx, item["id"], photos=[_photo("a"), _photo("c")])
    assert _updates_sent(relay_fake) == [("t1", _PHOTOS), ("t2", _PHOTOS)]


def test_a_price_drop_with_a_new_photo_is_one_message_per_buyer(make_ctx, store, bus, relay_fake):
    item, ctx = _asked_about_photo_lamp(make_ctx, store, bus, relay_fake)

    _edit(ctx, item["id"], list_price=80, photos=[_photo("a"), _photo("b"), _photo("c")])

    both = (
        f"The price of the Teak lamp is now $80 (was $95), and I've added new photos: "
        f"{_WEB}/listing/L1."
    )
    assert _updates_sent(relay_fake) == [("t1", both), ("t2", both)]
    assert _seller_hears(store) == [
        "📣 Teak lamp — let 2 buyers who asked know about the price drop and the new photos."
    ]


def test_a_photo_carousell_ai_could_not_take_tells_nobody(make_ctx, store, bus, relay_fake):
    # bazaar lists no upload tool while its media is off, so the listing keeps its old photos.
    item, ctx = _asked_about_photo_lamp(make_ctx, store, bus, relay_fake, client=RailClient)

    result = _edit(ctx, item["id"], photos=[_photo("a"), _photo("b"), _photo("c")])

    assert result["markets"]["carousell-ai"] == {"status": "updated"}
    assert relay_fake.reply_calls == []
    assert _seller_hears(store) == []


class _UploadFails(RailClient):
    """bazaar's media is on, but the photo upload itself fails."""

    def offers_tool(self, name: str) -> bool:
        return True

    def upload_photo(self, data: bytes, content_type: str) -> str:
        raise RailToolError("photo upload returned HTTP 503")


@pytest.mark.parametrize("first", [_UploadFails, RailClient], ids=["upload-failed", "media-off"])
def test_a_retry_that_lands_a_photo_tells_the_buyers(make_ctx, store, bus, relay_fake, first):
    item, ctx = _asked_about_photo_lamp(make_ctx, store, bus, relay_fake, client=first)
    new_set = [_photo("a"), _photo("b"), _photo("c")]
    _edit(ctx, item["id"], photos=new_set)
    assert relay_fake.reply_calls == []

    retry = _media_ctx(make_ctx, store, bus, relay_fake)
    result = _edit(retry, item["id"], photos=new_set)

    assert result["markets"]["carousell-ai"] == {"status": "updated"}
    assert _updates_sent(relay_fake) == [("t1", _PHOTOS), ("t2", _PHOTOS)]
