"""carousell_ai_publish_listing: composition order, fail-closed verify, idempotency."""

from __future__ import annotations

import pytest

import sellee.tools  # noqa: F401  registration
from sellee.rail.client import RailToolError, RailUnprovisioned
from sellee.tools.registry import TIER_PASS_PUBLISH, ToolError, dispatch


class FakeRail:
    """Records call order so a test can assert create precedes verify precedes record."""

    def __init__(self, *, url="https://www.carousell.ai/listing/1-lamp", verify_ok=True):
        self.url = url
        self.verify_ok = verify_ok
        self.args: dict = {}
        self.calls: list[str] = []

    def create_listing(self, args):
        self.args = dict(args)
        self.calls.append(f"create:{args['price_cents']}")
        return {"listing_id": "L1", "url": self.url}

    def verify_listing_url(self, url):
        self.calls.append(f"verify:{url}")
        if not self.verify_ok:
            raise RailToolError("listing page returned HTTP 404")


def _item(store, **kw):
    base = {"title": "Lamp", "list_price": 80.0}
    base.update(kw)
    return store.create_item(**base)


def _sells_in(store, country: str, currency: str = "") -> None:
    """Record what registration answered: where the seller sells, and in what currency."""
    basics = {"region": country}
    if currency:
        basics["currency"] = currency
    store.set_seller_config_section("basics", basics)


def test_publish_composes_create_verify_record_in_order(make_ctx, store) -> None:
    rail = FakeRail()
    ctx = make_ctx(TIER_PASS_PUBLISH, pass_id="p1", rail_factory=lambda: rail)
    _sells_in(store, "SG", "SGD")
    item = _item(store)
    result = dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert result == {"listing_id": "L1", "url": rail.url, "currency": "SGD"}
    assert rail.calls == ["create:8000", f"verify:{rail.url}"]  # money in code, verify after
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
    _sells_in(store, "SG", "SGD")
    item = _item(store, currency="SGD")
    first = dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)
    calls_after_first = list(rail.calls)
    second = dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)
    # Same keys either way: a caller reading `currency` must not get "" on the second call
    # merely because the listing already existed.
    assert second == {
        "listing_id": None,
        "url": first["url"],
        "already_published": True,
        "currency": "SGD",
    }
    assert first["currency"] == "SGD"
    assert rail.calls == calls_after_first  # never posted a second time


def test_publish_unprovisioned_names_the_fix(make_ctx, store) -> None:
    def factory():
        raise RailUnprovisioned("no key")

    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=factory)
    item = _item(store)
    with pytest.raises(ToolError, match="provision carousell-ai"):
        dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)


def test_publish_requires_a_price(make_ctx, store) -> None:
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: FakeRail())
    no_price = store.create_item(title="Lamp", list_price=None)
    with pytest.raises(ToolError, match="list price"):
        dispatch("carousell_ai_publish_listing", {"item_id": no_price["id"]}, ctx)


def test_publish_asserts_the_currency_registration_recorded(make_ctx, store) -> None:
    # The recorded code is authoritative, so sending it turns a disagreement into a refusal
    # before any listing exists rather than a live listing in the wrong currency.
    _sells_in(store, "VN", "VND")
    rail = FakeRail()
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    item = store.create_item(title="Bicycle", list_price=500.0)

    result = dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert rail.args["currency"] == "VND"
    assert result["currency"] == "VND"


def test_publish_asserts_nothing_when_no_currency_was_recorded(make_ctx, store) -> None:
    # A seller provisioned before registration answered has no code to send. The field is
    # optional, so omitting it cannot be refused.
    _sells_in(store, "VN")
    rail = FakeRail()
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    item = store.create_item(title="Bicycle", list_price=500.0)

    result = dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert "currency" not in rail.args
    assert result["currency"] == ""


def test_publish_never_relabels_the_item(make_ctx, store) -> None:
    """Nothing is read off the listing any more, so a published item keeps the currency the
    seller approved it in."""
    _sells_in(store, "VN", "VND")
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: FakeRail())
    item = store.create_item(title="Bicycle", list_price=500.0, currency="VND")

    dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert store.get_item(item["id"])["currency"] == "VND"


def test_a_backend_refusal_reaches_the_caller_in_its_own_words(make_ctx, store) -> None:
    """carousell.ai names both codes when it refuses an assertion. That message is the
    actionable one, so it is passed through rather than restated."""

    class RefusingRail(FakeRail):
        def create_listing(self, args):
            raise RailToolError("this seller's listings are priced in SGD; VND cannot be used")

    _sells_in(store, "VN", "VND")
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: RefusingRail())
    item = store.create_item(title="Bicycle", list_price=500.0, currency="VND")

    with pytest.raises(ToolError) as caught:
        dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert str(caught.value) == "this seller's listings are priced in SGD; VND cannot be used"
    assert store.get_item(item["id"])["listing_urls"] == {}


# --- the currency gate --------------------------------------------------------------------------


def test_publish_refuses_a_price_in_a_currency_the_seller_does_not_price_in(
    make_ctx, store
) -> None:
    """A price is a number the seller approved in a currency. Publishing 500 USD to an account
    that prices in VND would list it as 500 VND, so it is refused before the listing exists."""
    _sells_in(store, "VN", "VND")
    rail = FakeRail()
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    item = store.create_item(title="Bicycle", list_price=500.0, currency="USD")

    with pytest.raises(ToolError) as caught:
        dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    message = str(caught.value)
    assert "USD" in message and "VND" in message  # both, so the seller can see the swap
    assert rail.calls == []  # nothing was created
    assert store.get_item(item["id"])["listing_urls"] == {}
    assert store.get_item(item["id"])["currency"] == "USD"  # and nothing was relabelled


def test_the_gate_runs_before_anything_is_reserved(make_ctx, store) -> None:
    # A refusal must not burn an hourly pacing slot on a publish the backend would refuse too.
    _sells_in(store, "VN", "VND")
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: FakeRail())
    item = store.create_item(title="Bicycle", list_price=500.0, currency="USD")

    with pytest.raises(ToolError):
        dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    rows = store._db.query("SELECT ts FROM pacing_actions WHERE marketplace = ?", ("carousell-ai",))
    assert rows == []


def test_publish_allows_a_price_in_the_currency_the_seller_prices_in(make_ctx, store) -> None:
    _sells_in(store, "VN", "VND")
    rail = FakeRail()
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    item = store.create_item(title="Bicycle", list_price=500.0, currency="vnd")

    result = dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert result["url"] == rail.url


def test_a_seller_with_no_currency_recorded_is_not_gated(make_ctx, store) -> None:
    """A seller provisioned before registration answered has no recorded code. Asserting one
    would be the false refusal this gate exists to avoid."""
    _sells_in(store, "SG")
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: FakeRail())
    item = store.create_item(title="Lamp", list_price=80.0, currency="MYR")

    assert dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)["url"]


def test_an_item_with_no_currency_is_not_gated(make_ctx, store) -> None:
    # Nothing to contradict: this is the ordinary path.
    _sells_in(store, "VN", "VND")
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: FakeRail())
    item = store.create_item(title="Bicycle", list_price=500.0)

    assert dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)["url"]


def test_publish_missing_item_errors(make_ctx) -> None:
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: FakeRail())
    with pytest.raises(ToolError, match="no item"):
        dispatch("carousell_ai_publish_listing", {"item_id": "item_nope"}, ctx)
