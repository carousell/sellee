"""carousell_ai_publish_listing: composition order, fail-closed verify, idempotency."""

from __future__ import annotations

import pytest

import sellee.tools  # noqa: F401  registration
from sellee.rail.client import RailToolError, RailUnprovisioned
from sellee.tools.registry import TIER_PASS_CHANNEL, TIER_PASS_PUBLISH, ToolError, dispatch


class FakeRail:
    """Records call order so a test can assert create precedes verify precedes record."""

    def __init__(
        self, *, url="https://www.carousell.ai/listing/1-lamp", verify_ok=True, create_error=None
    ):
        self.url = url
        self.verify_ok = verify_ok
        self.calls: list[str] = []
        self._create_error = create_error

    def create_listing(self, args):
        self.calls.append(f"create:{args['price_cents']}:{args['currency']}")
        if self._create_error is not None:
            raise self._create_error
        return {"listing_id": "L1", "url": self.url}

    def verify_listing_url(self, url):
        self.calls.append(f"verify:{url}")
        if not self.verify_ok:
            raise RailToolError("listing page returned HTTP 404")


def _item(store, **kw):
    base = {"title": "Lamp", "list_price": 80.0, "currency": "SGD"}
    base.update(kw)
    return store.create_item(**base)


def test_publish_composes_create_verify_record_in_order(make_ctx, store) -> None:
    rail = FakeRail()
    ctx = make_ctx(TIER_PASS_PUBLISH, pass_id="p1", rail_factory=lambda: rail)
    item = _item(store)
    result = dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert result == {"listing_id": "L1", "url": rail.url}
    assert rail.calls == ["create:8000:SGD", f"verify:{rail.url}"]  # money in code, verify after
    assert store.get_item(item["id"])["listing_urls"]["carousell-ai"] == rail.url


def test_publish_fail_closed_verify_records_nothing(make_ctx, store) -> None:
    rail = FakeRail(verify_ok=False)
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    item = _item(store)
    with pytest.raises(ToolError, match="404"):
        dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)
    assert store.get_item(item["id"])["listing_urls"] == {}  # no URL recorded on a failed verify


def test_publish_is_idempotent(make_ctx, store) -> None:
    rail = FakeRail()
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    item = _item(store)
    first = dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)
    calls_after_first = list(rail.calls)
    second = dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)
    assert second == {"listing_id": None, "url": first["url"], "already_published": True}
    assert rail.calls == calls_after_first  # never posted a second time


def test_publish_unprovisioned_names_the_fix(make_ctx, store) -> None:
    def factory():
        raise RailUnprovisioned("no key")

    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=factory)
    item = _item(store)
    with pytest.raises(ToolError, match="provision carousell-ai"):
        dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)


def test_publish_requires_price_and_currency(make_ctx, store) -> None:
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: FakeRail())
    no_currency = store.create_item(title="Lamp", list_price=80.0)
    with pytest.raises(ToolError, match="currency"):
        dispatch("carousell_ai_publish_listing", {"item_id": no_currency["id"]}, ctx)


def test_publish_missing_item_errors(make_ctx) -> None:
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: FakeRail())
    with pytest.raises(ToolError, match="no item"):
        dispatch("carousell_ai_publish_listing", {"item_id": "item_nope"}, ctx)


# --- the unplaced-seller wall ---------------------------------------------------------------

_NO_MARKET_REFUSAL = (
    "seller has no market; the seller must confirm where they sell before this can continue"
)


def _refused_publish(make_ctx, store, tier):
    rail = FakeRail(create_error=RailToolError(_NO_MARKET_REFUSAL))
    ctx = make_ctx(tier, rail_factory=lambda: rail)
    item = _item(store)
    with pytest.raises(ToolError) as exc:
        dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)
    assert store.get_item(item["id"])["listing_urls"] == {}  # nothing recorded
    return str(exc.value)


def test_publish_asks_the_seller_where_they_sell(make_ctx, store) -> None:
    """Publishing is the FIRST wall an unplaced seller meets — listing is refused long before a
    checkout link is asked for — so the ask has to live here too, not only on checkout."""
    message = _refused_publish(make_ctx, store, TIER_PASS_CHANNEL)
    assert "Singapore" in message
    assert "United States" in message
    assert "carousell_ai_set_market" in message


def test_publish_pass_escalates_because_it_talks_to_no_one(make_ctx, store) -> None:
    """The publish pass has no channel to the seller and cannot see carousell_ai_set_market, so
    naming either would send it at something it cannot do."""
    message = _refused_publish(make_ctx, store, TIER_PASS_PUBLISH)
    assert "escalate" in message
    assert "carousell_ai_set_market" not in message


def test_publish_keeps_the_generic_wrap_for_other_refusals(make_ctx, store) -> None:
    rail = FakeRail(create_error=RailToolError("listing is not active"))
    ctx = make_ctx(TIER_PASS_CHANNEL, rail_factory=lambda: rail)
    item = _item(store)
    with pytest.raises(ToolError, match="listing is not active"):
        dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)
