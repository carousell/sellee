"""carousell_ai_publish_listing: composition order, fail-closed verify, idempotency."""

from __future__ import annotations

import pytest

import sellee.tools  # noqa: F401  registration
from sellee.rail.client import RailNetworkError, RailToolError, RailUnprovisioned
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_PUBLISH,
    ToolError,
    dispatch,
)


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


def _item(store, *, size_bucket="m", **kw):
    """A publishable item: a draft carrying the parcel size class publish requires."""
    base = {"title": "Lamp", "list_price": 80.0, "size_bucket": size_bucket}
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

    assert result == {"listing_id": "L1", "url": rail.url}
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
    assert second == {"listing_id": None, "url": first["url"], "already_published": True}
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
    no_price = _item(store, title="Lamp", list_price=None)
    with pytest.raises(ToolError, match="list price"):
        dispatch("carousell_ai_publish_listing", {"item_id": no_price["id"]}, ctx)


def test_publish_asserts_the_currency_registration_recorded(make_ctx, store) -> None:
    # The recorded code is authoritative, so sending it turns a disagreement into a refusal
    # before any listing exists rather than a live listing in the wrong currency.
    _sells_in(store, "VN", "VND")
    rail = FakeRail()
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    item = _item(store, title="Bicycle", list_price=500.0)

    result = dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert rail.args["currency"] == "VND"
    assert result["url"] == rail.url


def test_publish_asserts_nothing_when_no_currency_was_recorded(make_ctx, store) -> None:
    # A seller provisioned before registration answered has no code to send. The field is
    # optional, so omitting it cannot be refused.
    _sells_in(store, "VN")
    rail = FakeRail()
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    item = _item(store, title="Bicycle", list_price=500.0)

    result = dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert "currency" not in rail.args
    assert result["url"] == rail.url


def test_publish_never_relabels_the_item(make_ctx, store) -> None:
    """Nothing is read off the listing any more, so a published item keeps the currency the
    seller approved it in."""
    _sells_in(store, "VN", "VND")
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: FakeRail())
    item = _item(store, title="Bicycle", list_price=500.0, currency="VND")

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
    item = _item(store, title="Bicycle", list_price=500.0, currency="VND")

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
    item = _item(store, title="Bicycle", list_price=500.0, currency="USD")

    with pytest.raises(ToolError) as caught:
        dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    message = str(caught.value)
    assert "USD" in message and "VND" in message  # both, so the seller can see the swap
    assert rail.calls == []  # nothing was created
    assert store.get_item(item["id"])["listing_urls"] == {}
    assert store.get_item(item["id"])["currency"] == "USD"  # and nothing was relabelled


def test_the_gate_runs_before_anything_is_reserved(make_ctx, store) -> None:
    # A refusal must not consume an hourly pacing slot on a publish the backend would refuse too.
    _sells_in(store, "VN", "VND")
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: FakeRail())
    item = _item(store, title="Bicycle", list_price=500.0, currency="USD")

    with pytest.raises(ToolError):
        dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    rows = store._db.query("SELECT ts FROM pacing_actions WHERE marketplace = ?", ("carousell-ai",))
    assert rows == []


def test_publish_allows_a_price_in_the_currency_the_seller_prices_in(make_ctx, store) -> None:
    _sells_in(store, "VN", "VND")
    rail = FakeRail()
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    item = _item(store, title="Bicycle", list_price=500.0, currency="vnd")

    result = dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert result["url"] == rail.url


def test_a_seller_with_no_currency_recorded_is_not_gated(make_ctx, store) -> None:
    """A seller provisioned before registration answered has no recorded code. Asserting one
    would be the false refusal this gate exists to avoid."""
    _sells_in(store, "SG")
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: FakeRail())
    item = _item(store, title="Lamp", list_price=80.0, currency="MYR")

    assert dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)["url"]


def test_an_item_with_no_currency_is_not_gated(make_ctx, store) -> None:
    # Nothing to contradict: this is the ordinary path.
    _sells_in(store, "VN", "VND")
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: FakeRail())
    item = _item(store, title="Bicycle", list_price=500.0)

    assert dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)["url"]


def test_publish_missing_item_errors(make_ctx) -> None:
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: FakeRail())
    with pytest.raises(ToolError, match="no item"):
        dispatch("carousell_ai_publish_listing", {"item_id": "item_nope"}, ctx)


# --- the parcel size class ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "sent"),
    [
        ("s", "PARCEL_BUCKET_S"),
        ("m", "PARCEL_BUCKET_M"),
        ("l", "PARCEL_BUCKET_L"),
        ("xl", "PARCEL_BUCKET_XL"),
    ],
)
def test_publish_sends_the_stored_class_as_the_rail_enum(make_ctx, store, stored, sent) -> None:
    rail = FakeRail()
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    item = _item(store, size_bucket=stored)

    dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert rail.args["parcel_bucket"] == sent


def test_publish_sends_a_class_the_seller_corrected_later(make_ctx, store) -> None:
    """The draft carries the agent's pick; the seller's answer replaces it, and that is what the
    listing is created with."""
    rail = FakeRail()
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    item = _item(store, size_bucket="m")
    store.update_item(item["id"], {"size_bucket": "l"})

    dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert rail.args["parcel_bucket"] == "PARCEL_BUCKET_L"


def test_publish_refuses_an_item_with_no_class_before_reserving(make_ctx, store) -> None:
    """The listing could not be created without one, so the refusal comes early — it names the
    tool that sets the class, and costs no hourly pacing slot."""
    rail = FakeRail()
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: rail)
    item = _item(store, size_bucket=None)

    with pytest.raises(ToolError) as caught:
        dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert "size_bucket" in str(caught.value) and "update_item" in str(caught.value)
    assert rail.calls == []
    rows = store._db.query("SELECT ts FROM pacing_actions WHERE marketplace = ?", ("carousell-ai",))
    assert rows == []


# --- the sign-in offered as a listing goes live -------------------------------------------------

_SIGNIN_URL = "https://www.carousell.ai/signin?flow=guest-promotion&promote=tok"


class GuestRail(FakeRail):
    """A rail whose account is still a guest, so it mints a sign-in link when asked."""

    def __init__(self, *, signin_url=_SIGNIN_URL, signin_error=None, **kw):
        super().__init__(**kw)
        self.signin_url = signin_url
        self.signin_error = signin_error
        self.signin_calls = 0

    def create_promotion_url(self):
        self.signin_calls += 1
        if self.signin_error is not None:
            raise self.signin_error
        return {"promotion_url": self.signin_url}


def _publish_as(make_ctx, store, rail, *, tier=TIER_PASS_CHANNEL, region="US", currency="USD"):
    _sells_in(store, region, currency)
    item = _item(store, currency=currency)
    ctx = make_ctx(tier, rail_factory=lambda: rail)
    return dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx), item


def test_a_us_guest_is_offered_the_signin_as_their_listing_goes_live(make_ctx, store) -> None:
    """Signing in and adding a ZIP is what lets a buyer pay, so it is offered the moment there is
    something to pay for, rather than only once a buyer is already waiting."""
    result, _ = _publish_as(make_ctx, store, GuestRail())

    assert result["url"] == "https://www.carousell.ai/listing/1-lamp"
    assert result["signin_offer"] == (
        f"Add your ZIP now so buyers can pay you right away: {_SIGNIN_URL} It takes a "
        "minute: confirm your email, then the ZIP code you ship from. The link is live for "
        "~15 minutes; if it expires, just ask and I'll send a fresh one."
    )


def test_the_attended_session_is_offered_it_too(make_ctx, store) -> None:
    result, _ = _publish_as(make_ctx, store, GuestRail(), tier=TIER_ATTENDED)

    assert _SIGNIN_URL in result["signin_offer"]


def test_a_signed_in_seller_is_offered_nothing(make_ctx, store) -> None:
    rail = GuestRail(signin_error=RailToolError("already a seller"))

    result, _ = _publish_as(make_ctx, store, rail)

    assert "signin_offer" not in result
    assert rail.signin_calls == 1


def test_a_seller_outside_the_checkout_region_is_never_asked(make_ctx, store) -> None:
    """carousell.ai takes payments in the United States only, so elsewhere the promise is false."""
    rail = GuestRail()

    result, _ = _publish_as(make_ctx, store, rail, region="SG", currency="SGD")

    assert "signin_offer" not in result
    assert rail.signin_calls == 0


def test_the_publish_pass_never_holds_a_signin_link(make_ctx, store) -> None:
    """That pass talks to no one, and the link hands over the seller's account."""
    rail = GuestRail()

    result, _ = _publish_as(make_ctx, store, rail, tier=TIER_PASS_PUBLISH)

    assert "signin_offer" not in result
    assert rail.signin_calls == 0


def test_a_failed_offer_still_publishes(make_ctx, store, caplog) -> None:
    """The listing is live and recorded by then, so a failed mint must not fail the publish. The
    log says why it failed."""
    rail = GuestRail(signin_error=RailNetworkError("rail unreachable"))

    result, item = _publish_as(make_ctx, store, rail)

    assert result["url"] == rail.url
    assert "signin_offer" not in result
    assert store.get_item(item["id"])["listing_urls"]["carousell-ai"] == rail.url
    assert "could not mint the sign-in offered at publish: rail unreachable" in caplog.text


def test_a_signin_link_off_the_sign_in_base_is_never_offered(make_ctx, store, caplog) -> None:
    rail = GuestRail(signin_url="https://evil.example/signin?promote=tok")

    result, _ = _publish_as(make_ctx, store, rail)

    assert "signin_offer" not in result
    # The refusal is logged, never the link: it hands over whoever's account it names.
    assert "could not mint" in caplog.text
    assert "promote=tok" not in caplog.text


def test_a_signin_link_that_could_forge_a_line_is_never_offered(make_ctx, store) -> None:
    rail = GuestRail(signin_url=f"{_SIGNIN_URL}\nTap here instead: https://evil.example")

    result, _ = _publish_as(make_ctx, store, rail)

    assert "signin_offer" not in result


def test_a_repeated_publish_does_not_ask_again(make_ctx, store) -> None:
    """The offer went out with the first publish, so a retry must not ask again or call the
    rail."""
    rail = GuestRail()
    _sells_in(store, "US", "USD")
    item = _item(store, currency="USD")
    ctx = make_ctx(TIER_PASS_CHANNEL, rail_factory=lambda: rail)
    dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    again = dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)

    assert again == {"listing_id": None, "url": rail.url, "already_published": True}
    assert rail.signin_calls == 1
