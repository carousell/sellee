"""carousell_ai_create_checkout_link: floor gate (no value leaked), sale-id idempotency,
listing_id resolution, fail-closed URL base, and the not_published / no_floor / below_floor paths.
The legacy 'listable' self-heal is intentionally not ported."""

from __future__ import annotations

import pytest

from sellee.config import Config
from sellee.tools.registry import TIER_ATTENDED, ToolError, dispatch

_CFG = Config(carousell_ai_api_base="https://api.carousell.ai")
_URL = "https://api.carousell.ai/checkout/abc123?listing_id=L1"


class FakeRail:
    def __init__(self, *, url=_URL):
        self.url = url
        self.calls = 0

    def create_checkout(self, args):
        self.calls += 1
        return {"checkout_url": self.url}


def _published_item(store, *, list_price=100.0, floor=None):
    item = store.create_item(title="Lamp", list_price=list_price, currency="SGD")
    if floor is not None:
        store.set_floor(item["id"], floor, "seller")
    store.record_listing_url(item["id"], "carousell-ai", "https://www.carousell.ai/listing/L1")
    return item


def _ctx(make_ctx, rail):
    return make_ctx(TIER_ATTENDED, rail_factory=lambda: rail, config=_CFG)


def test_mints_at_or_above_floor_and_records(make_ctx, store) -> None:
    item = _published_item(store, list_price=100.0, floor=60.0)
    rail = FakeRail()
    res = dispatch(
        "carousell_ai_create_checkout_link",
        {"item_id": item["id"], "thread_id": "fb:1", "agreed_price": 90.0},
        _ctx(make_ctx, rail),
    )
    assert res["checkout_url"] == _URL
    assert rail.calls == 1


def test_idempotent_second_call_returns_existing_link(make_ctx, store) -> None:
    item = _published_item(store, list_price=100.0, floor=60.0)
    rail = FakeRail()
    ctx = _ctx(make_ctx, rail)
    first = dispatch(
        "carousell_ai_create_checkout_link",
        {"item_id": item["id"], "thread_id": "fb:1", "agreed_price": 90.0},
        ctx,
    )
    second = dispatch(
        "carousell_ai_create_checkout_link",
        {"item_id": item["id"], "thread_id": "fb:1", "agreed_price": 90.0},
        ctx,
    )
    assert second["already_issued"] is True
    assert second["checkout_url"] == first["checkout_url"]
    assert rail.calls == 1  # never minted a second link


def test_below_floor_refused_without_leaking_value(make_ctx, store) -> None:
    item = _published_item(store, list_price=100.0, floor=80.0)
    with pytest.raises(ToolError) as exc:
        dispatch(
            "carousell_ai_create_checkout_link",
            {"item_id": item["id"], "thread_id": "fb:1", "agreed_price": 70.0},
            _ctx(make_ctx, FakeRail()),
        )
    assert "80" not in str(exc.value)  # the floor value never appears in the error


def test_floorless_at_or_above_list_writes_default_and_mints(make_ctx, store) -> None:
    item = _published_item(store, list_price=100.0)  # no floor
    res = dispatch(
        "carousell_ai_create_checkout_link",
        {"item_id": item["id"], "thread_id": "fb:1", "agreed_price": 100.0},
        _ctx(make_ctx, FakeRail()),
    )
    assert res["checkout_url"] == _URL
    assert store.get_floor(item["id"])["source"] == "default"


def test_floorless_below_list_returns_no_floor(make_ctx, store) -> None:
    item = _published_item(store, list_price=100.0)  # no floor
    with pytest.raises(ToolError, match="ask the seller"):
        dispatch(
            "carousell_ai_create_checkout_link",
            {"item_id": item["id"], "thread_id": "fb:1", "agreed_price": 80.0},
            _ctx(make_ctx, FakeRail()),
        )


def test_unpublished_item_returns_not_published(make_ctx, store) -> None:
    item = store.create_item(title="Lamp", list_price=100.0, currency="SGD")
    store.set_floor(item["id"], 60.0, "seller")  # floor fine, just not published
    with pytest.raises(ToolError, match="publish it first"):
        dispatch(
            "carousell_ai_create_checkout_link",
            {"item_id": item["id"], "thread_id": "fb:1", "agreed_price": 90.0},
            _ctx(make_ctx, FakeRail()),
        )


def test_foreign_url_base_rejected(make_ctx, store) -> None:
    item = _published_item(store, list_price=100.0, floor=60.0)
    rogue = FakeRail(url="https://evil.example/checkout/abc")
    with pytest.raises(ToolError, match="checkout base"):
        dispatch(
            "carousell_ai_create_checkout_link",
            {"item_id": item["id"], "thread_id": "fb:1", "agreed_price": 90.0},
            _ctx(make_ctx, rogue),
        )
    assert store._db.query("SELECT COUNT(*) AS n FROM checkouts")[0]["n"] == 0  # nothing recorded


def test_missing_item_errors(make_ctx, store) -> None:
    with pytest.raises(ToolError, match="no item"):
        dispatch(
            "carousell_ai_create_checkout_link",
            {"item_id": "item_nope", "thread_id": "fb:1", "agreed_price": 90.0},
            _ctx(make_ctx, FakeRail()),
        )


# --- the scam gate --------------------------------------------------------------------------------


def test_no_link_is_minted_on_a_thread_carrying_a_scam_signal(make_ctx, store) -> None:
    """The one value-moving tool in the reply tier, refused server-side on the engine's verdict."""
    item = _published_item(store, list_price=100.0, floor=60.0)
    store.create_thread(
        thread_id="carousell:1",
        side="sell",
        market="carousell",
        counterpart_handle="bob",
        item_id=item["id"],
    )
    store.record_inbound(
        "carousell:1", msg_id="m1", text="pay via this link", ts=10.0, scam_verdict="scam"
    )
    rail = FakeRail()
    with pytest.raises(ToolError, match="scam signal"):
        dispatch(
            "carousell_ai_create_checkout_link",
            {"item_id": item["id"], "thread_id": "carousell:1", "agreed_price": 90.0},
            _ctx(make_ctx, rail),
        )
    # refused before the rail is touched, so no link exists to be leaked
    assert rail.calls == 0


def test_a_mint_still_works_without_a_thread_row(make_ctx, store) -> None:
    """A mint has never required a thread to exist; the scam gate must not add that requirement."""
    item = _published_item(store, list_price=100.0, floor=60.0)
    rail = FakeRail()
    res = dispatch(
        "carousell_ai_create_checkout_link",
        {"item_id": item["id"], "thread_id": "carousell:absent", "agreed_price": 90.0},
        _ctx(make_ctx, rail),
    )
    assert res["checkout_url"] == _URL
