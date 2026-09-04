"""carousell_ai_create_checkout_link: floor gate (no value leaked), sale-id idempotency,
listing_id resolution, fail-closed URL base, and the not_published / no_floor / below_floor paths.
The legacy 'listable' self-heal is intentionally not ported.

Plus the guest-account wall in front of it and its remedy, carousell_ai_create_signin_link: the
refusal is worded per tier (the buyer-facing reply tier is never told a tool it cannot see), and the
sign-in URL is validated fail-closed because it grants ownership of the seller's account."""

from __future__ import annotations

import pytest

from sellee.config import Config
from sellee.rail.client import RailNetworkError, RailToolError, RailUnprovisioned
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_REPLY,
    ToolError,
    UnknownTool,
    dispatch,
)

_CFG = Config(carousell_ai_api_base="https://api.carousell.ai")
_URL = "https://www.carousell.ai/checkout/abc123?listing_id=L1"
_GUEST_REFUSAL = (
    "checkout is unavailable for this listing: it belongs to a guest account, and the seller must "
    "sign in before checkout links can be created"
)
_SIGNIN_URL = "https://www.carousell.ai/signin?flow=guest-promotion&promote=tok"
_NO_MARKET_REFUSAL = (
    "seller has no market; the seller must confirm where they sell before this can continue"
)


class FakeRail:
    def __init__(self, *, url=_URL, checkout_error=None):
        self.url = url
        self.calls = 0
        self._checkout_error = checkout_error

    def create_checkout(self, args):
        self.calls += 1
        if self._checkout_error is not None:
            raise self._checkout_error
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


def test_idempotent_return_survives_a_later_floor_increase(make_ctx, store) -> None:
    """set_floor(force=True) has no awareness of existing checkouts. If the seller raises the
    floor after a deal already closed above the old floor, re-fetching that already-issued link
    must not re-run the floor gate against the new floor — nothing about the recorded sale needs
    re-validating."""
    item = _published_item(store, list_price=100.0, floor=50.0)
    rail = FakeRail()
    ctx = _ctx(make_ctx, rail)
    first = dispatch(
        "carousell_ai_create_checkout_link",
        {"item_id": item["id"], "thread_id": "fb:1", "agreed_price": 60.0},
        ctx,
    )
    store.set_floor(item["id"], 70.0, "seller", force=True)
    second = dispatch(
        "carousell_ai_create_checkout_link",
        {"item_id": item["id"], "thread_id": "fb:1", "agreed_price": 60.0},
        ctx,
    )
    assert second["already_issued"] is True
    assert second["checkout_url"] == first["checkout_url"]
    assert rail.calls == 1  # never re-minted


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


def test_checkout_base_is_the_web_origin_not_the_api_origin(make_ctx, store) -> None:
    """Real checkout pages are served on the web origin; a link on the API origin (the old,
    wrong base — only the coming-soon demo ever minted there) must be rejected."""
    item = _published_item(store, list_price=100.0, floor=60.0)
    api_hosted = FakeRail(url="https://api.carousell.ai/checkout/abc123?listing_id=L1")
    with pytest.raises(ToolError, match="checkout base"):
        dispatch(
            "carousell_ai_create_checkout_link",
            {"item_id": item["id"], "thread_id": "fb:1", "agreed_price": 90.0},
            _ctx(make_ctx, api_hosted),
        )


def test_missing_item_errors(make_ctx, store) -> None:
    with pytest.raises(ToolError, match="no item"):
        dispatch(
            "carousell_ai_create_checkout_link",
            {"item_id": "item_nope", "thread_id": "fb:1", "agreed_price": 90.0},
            _ctx(make_ctx, FakeRail()),
        )


# --- the guest-account wall ----------------------------------------------------------------------


def _refused(make_ctx, store, tier, error):
    item = _published_item(store, list_price=100.0, floor=60.0)
    rail = FakeRail(checkout_error=error)
    ctx = make_ctx(tier, rail_factory=lambda: rail, config=_CFG)
    with pytest.raises(ToolError) as exc:
        dispatch(
            "carousell_ai_create_checkout_link",
            {"item_id": item["id"], "thread_id": "fb:1", "agreed_price": 90.0},
            ctx,
        )
    return str(exc.value)


def test_guest_refusal_points_the_seller_channel_at_the_signin_tool(make_ctx, store) -> None:
    message = _refused(make_ctx, store, TIER_PASS_CHANNEL, RailToolError(_GUEST_REFUSAL))
    assert "carousell_ai_create_signin_link" in message
    assert "sign-in" in message


def test_guest_refusal_never_names_the_signin_tool_to_a_reply_pass(make_ctx, store) -> None:
    """The reply tier cannot see that tool, so naming it would send the pass at an unknown tool;
    and the buyer must never learn the seller's account status."""
    message = _refused(make_ctx, store, TIER_PASS_REPLY, RailToolError(_GUEST_REFUSAL))
    assert "carousell_ai_create_signin_link" not in message
    assert "escalate" in message
    assert "holding line" in message


def test_guest_refusal_records_nothing_so_a_retry_mints_fresh(make_ctx, store) -> None:
    _refused(make_ctx, store, TIER_PASS_CHANNEL, RailToolError(_GUEST_REFUSAL))
    assert store._db.query("SELECT COUNT(*) AS n FROM checkouts")[0]["n"] == 0


def test_missing_market_asks_the_seller_where_they_sell(make_ctx, store) -> None:
    """A seller who signed up on the web is left unplaced on purpose — carousell.ai runs one
    payout account per market and the assignment is permanent — so the remedy is to ask, not to
    retry."""
    message = _refused(make_ctx, store, TIER_PASS_CHANNEL, RailToolError(_NO_MARKET_REFUSAL))
    assert "Singapore" in message
    assert "United States" in message
    assert "permanent" in message


def test_missing_market_never_tells_the_buyer_about_the_seller(make_ctx, store) -> None:
    message = _refused(make_ctx, store, TIER_PASS_REPLY, RailToolError(_NO_MARKET_REFUSAL))
    assert "escalate" in message
    assert "holding line" in message


def test_missing_market_records_nothing_so_a_retry_mints_fresh(make_ctx, store) -> None:
    _refused(make_ctx, store, TIER_PASS_CHANNEL, RailToolError(_NO_MARKET_REFUSAL))
    assert store._db.query("SELECT COUNT(*) AS n FROM checkouts")[0]["n"] == 0


def test_another_rail_tool_error_keeps_the_generic_wrap(make_ctx, store) -> None:
    message = _refused(make_ctx, store, TIER_PASS_CHANNEL, RailToolError("listing is not active"))
    assert message == "listing is not active"


def test_a_transport_failure_is_not_the_gate(make_ctx, store) -> None:
    message = _refused(make_ctx, store, TIER_PASS_CHANNEL, RailNetworkError("rail unreachable"))
    assert message == "rail unreachable"


# --- carousell_ai_create_signin_link -------------------------------------------------------------


class FakeSigninRail:
    def __init__(self, *, url=_SIGNIN_URL, error=None):
        self.url = url
        self.error = error
        self.calls = 0

    def create_promotion_url(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"promotion_url": self.url}


def _signin(make_ctx, rail, tier=TIER_PASS_CHANNEL):
    return dispatch(
        "carousell_ai_create_signin_link",
        {},
        make_ctx(tier, rail_factory=lambda: rail, config=_CFG),
    )


def test_signin_link_is_minted_and_returned(make_ctx, store) -> None:
    rail = FakeSigninRail()
    assert _signin(make_ctx, rail) == {"signin_url": _SIGNIN_URL}
    assert rail.calls == 1


def test_signin_link_off_the_configured_web_base_is_rejected(make_ctx, store) -> None:
    """This URL hands over the seller's account — an unexpected host is never forwarded."""
    rogue = FakeSigninRail(url="https://evil.example/signin?promote=tok")
    with pytest.raises(ToolError, match="sign-in base"):
        _signin(make_ctx, rogue)


def test_signin_link_on_the_wrong_path_is_rejected(make_ctx, store) -> None:
    with pytest.raises(ToolError, match="sign-in base"):
        _signin(make_ctx, FakeSigninRail(url="https://www.carousell.ai/u/chat"))


def test_signin_link_needs_a_path_boundary_after_signin(make_ctx, store) -> None:
    """`/signin` must end there or continue past a `?` or `/` — `/signinfoo` is not it."""
    with pytest.raises(ToolError, match="sign-in base"):
        _signin(make_ctx, FakeSigninRail(url="https://www.carousell.ai/signinfoo?promote=tok"))
    bare = FakeSigninRail(url="https://www.carousell.ai/signin")
    assert _signin(make_ctx, bare) == {"signin_url": "https://www.carousell.ai/signin"}


def test_already_a_seller_is_a_success_that_routes_to_checkout(make_ctx, store) -> None:
    rail = FakeSigninRail(error=RailToolError("already a seller"))
    result = _signin(make_ctx, rail)
    assert result["already_signed_in"] is True
    assert "checkout link" in result["note"]


def test_other_rail_failures_surface_as_tool_errors(make_ctx, store) -> None:
    with pytest.raises(ToolError, match="rail unreachable"):
        _signin(make_ctx, FakeSigninRail(error=RailNetworkError("rail unreachable")))


def test_signin_link_without_a_provisioned_rail_asks_for_provisioning(make_ctx, store) -> None:
    def factory():
        raise RailUnprovisioned("no key")

    ctx = make_ctx(TIER_PASS_CHANNEL, rail_factory=factory, config=_CFG)
    with pytest.raises(ToolError, match="not provisioned"):
        dispatch("carousell_ai_create_signin_link", {}, ctx)


def test_signin_link_without_a_rail_at_all_says_so(make_ctx, store) -> None:
    ctx = make_ctx(TIER_PASS_CHANNEL, config=_CFG)
    with pytest.raises(ToolError, match="not available"):
        dispatch("carousell_ai_create_signin_link", {}, ctx)


def test_signin_link_is_invisible_to_the_reply_pass(make_ctx, store) -> None:
    """Omission is the structural guard: a buyer-facing pass must not be able to hold this URL."""
    ctx = make_ctx(TIER_PASS_REPLY, rail_factory=lambda: FakeSigninRail(), config=_CFG)
    with pytest.raises(UnknownTool):
        dispatch("carousell_ai_create_signin_link", {}, ctx)
