"""carousell_ai_publish_listing: composition order, fail-closed verify, idempotency."""

from __future__ import annotations

import pytest

import sellee.tools  # noqa: F401  registration
from sellee.rail.client import RailToolError, RailUnprovisioned
from sellee.tools.registry import TIER_PASS_PUBLISH, ToolError, dispatch


class FakeRail:
    """Records call order so a test can assert create precedes verify precedes record."""

    def __init__(
        self, *, url="https://www.carousell.ai/listing/1-lamp", verify_ok=True, currency="SGD"
    ):
        self.url = url
        self.verify_ok = verify_ok
        self.currency = currency
        self.args: dict = {}
        self.calls: list[str] = []

    def create_listing(self, args):
        self.args = dict(args)
        self.calls.append(f"create:{args['price_cents']}")
        return {"listing_id": "L1", "url": self.url, "currency": self.currency}

    def verify_listing_url(self, url):
        self.calls.append(f"verify:{url}")
        if not self.verify_ok:
            raise RailToolError("listing page returned HTTP 404")


def _item(store, **kw):
    base = {"title": "Lamp", "list_price": 80.0}
    base.update(kw)
    return store.create_item(**base)


def _sells_in(store, country: str) -> None:
    """Record where the seller sells, which is what the currency gate reads."""
    store.set_seller_config_section("basics", {"region": country})


def test_publish_composes_create_verify_record_in_order(make_ctx, store) -> None:
    rail = FakeRail()
    ctx = make_ctx(TIER_PASS_PUBLISH, pass_id="p1", rail_factory=lambda: rail)
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
    item = _item(store)
    first = dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)
    calls_after_first = list(rail.calls)
    second = dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)
    assert second == {"listing_id": None, "url": first["url"], "already_published": True}
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


def test_publish_never_asserts_a_currency(make_ctx, store) -> None:
    # An unplaced seller has no currency to assert, and asserting the wrong one is refused by
    # bazaar. Omitting the field cannot be refused, so the field is never sent at all.
    rail = FakeRail(currency="VND")
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    item = store.create_item(title="Bicycle", list_price=500.0)

    result = dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert "currency" not in rail.args
    assert result["currency"] == "VND"


def test_publish_fills_in_the_currency_for_an_item_that_had_none(make_ctx, store) -> None:
    # The listing carousell.ai created is the authority on what it is priced in, and an item
    # with no currency has no approved amount to contradict.
    _sells_in(store, "VN")
    rail = FakeRail(currency="VND")
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    item = store.create_item(title="Bicycle", list_price=500.0)

    dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert store.get_item(item["id"])["currency"] == "VND"


def test_publish_never_relabels_a_currency_the_item_already_had(make_ctx, store) -> None:
    """The gate stops the usual mismatch before the listing exists. If carousell.ai still answers
    with a different code, the approved amount is left alone rather than reinterpreted."""
    _sells_in(store, "VN")
    rail = FakeRail(currency="THB")
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    item = store.create_item(title="Bicycle", list_price=500.0, currency="VND")

    result = dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert store.get_item(item["id"])["currency"] == "VND"  # 500 VND is still 500 VND
    assert result["currency"] == "THB"  # and the caller is told what the listing carries


def test_publish_leaves_the_item_alone_when_no_currency_comes_back(make_ctx, store) -> None:
    _sells_in(store, "SG")
    rail = FakeRail(currency="")
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")

    dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert store.get_item(item["id"])["currency"] == "SGD"


# --- the currency gate --------------------------------------------------------------------------


def test_publish_refuses_a_price_in_a_currency_the_seller_does_not_price_in(
    make_ctx, store
) -> None:
    """A price is a number the seller approved in a currency. Publishing 500 USD to an account
    that prices in VND would list it as 500 VND, so it is refused before the listing exists."""
    _sells_in(store, "VN")
    rail = FakeRail(currency="VND")
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    item = store.create_item(title="Bicycle", list_price=500.0, currency="USD")

    with pytest.raises(ToolError) as caught:
        dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    message = str(caught.value)
    assert "USD" in message and "VND" in message  # both, so the seller can see the swap
    assert rail.calls == []  # nothing was created
    assert store.get_item(item["id"])["listing_urls"] == {}
    assert store.get_item(item["id"])["currency"] == "USD"  # and nothing was relabelled


def test_publish_allows_a_price_in_the_currency_the_seller_prices_in(make_ctx, store) -> None:
    _sells_in(store, "VN")
    rail = FakeRail(currency="VND")
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    item = store.create_item(title="Bicycle", list_price=500.0, currency="vnd")

    result = dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert result["url"] == rail.url


def test_the_gate_reads_a_country_outside_the_table_as_usd(make_ctx, store) -> None:
    # Brazil has no entry, so carousell.ai prices a Brazilian seller's listing in USD. A BRL
    # price would be listed as USD, which is the same swap and is refused the same way.
    _sells_in(store, "BR")
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: FakeRail(currency="USD"))
    brl = store.create_item(title="Bicycle", list_price=500.0, currency="BRL")
    with pytest.raises(ToolError, match="USD"):
        dispatch("carousell_ai_publish_listing", {"item_id": brl["id"]}, ctx)

    usd = store.create_item(title="Lamp", list_price=80.0, currency="USD")
    dispatch("carousell_ai_publish_listing", {"item_id": usd["id"]}, ctx)  # no raise


def test_the_gate_holds_a_placed_seller_to_their_regions_currency(make_ctx, store) -> None:
    # A region is named by its country's code and settles in that country's currency, so the
    # two agree: an SG seller prices in SGD whether or not a platform account backs them.
    _sells_in(store, "SG")
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: FakeRail(currency="SGD"))
    item = store.create_item(title="Lamp", list_price=80.0, currency="MYR")
    with pytest.raises(ToolError, match="SGD"):
        dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)


def test_a_seller_with_no_country_recorded_is_not_gated(make_ctx, store) -> None:
    """The gate names what carousell.ai will use, so it needs a country to name it from. With
    none, guessing USD would refuse prices carousell.ai would have accepted."""
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: FakeRail(currency="SGD"))
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")

    assert dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)["currency"] == (
        "SGD"
    )


def test_an_item_with_no_currency_is_not_gated(make_ctx, store) -> None:
    # Nothing to contradict: this is the ordinary path, and the code arrives with the listing.
    _sells_in(store, "VN")
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: FakeRail(currency="VND"))
    item = store.create_item(title="Bicycle", list_price=500.0)
    assert dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)["currency"] == (
        "VND"
    )


def test_publish_missing_item_errors(make_ctx) -> None:
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: FakeRail())
    with pytest.raises(ToolError, match="no item"):
        dispatch("carousell_ai_publish_listing", {"item_id": "item_nope"}, ctx)
