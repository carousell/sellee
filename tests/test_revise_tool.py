"""update_live_listing and record_listing_revision: the whole edit in one call, one honest
verdict per marketplace, and the refusals that keep an edit from moving a deal under a buyer."""

from __future__ import annotations

import pytest
from tests.conftest import seed_setting

import sellee.tools  # noqa: F401  registration
from sellee import paths
from sellee.config import Config
from sellee.rail.client import RailToolError
from sellee.tools.registry import TIER_ATTENDED, TIER_PASS_CHANNEL, TIER_PASS_EDIT, ToolError
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
