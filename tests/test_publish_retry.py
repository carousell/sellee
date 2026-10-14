"""carousell_ai_publish_listing and the checkout link under retry, against a fake bazaar that can
make a listing and then drop its answer. sellee's idempotency key gets the first one back."""

from __future__ import annotations

import itertools

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.fake_carousell_ai_mcp import FakeRelay, serve

from sellee.config import Config
from sellee.rail.client import RailClient, RailToolError
from sellee.store import StoreError
from sellee.tools.registry import TIER_ATTENDED, ToolError, dispatch

_PROPERTY = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
_TITLES = itertools.count()


@pytest.fixture
def fake():
    relay_state = FakeRelay()
    _, shutdown = serve(relay_state)
    yield relay_state
    shutdown()


@pytest.fixture
def ctx(make_ctx, store, fake, monkeypatch):
    # Pacing is its own seam; a run of retries here must not meet the hourly cap.
    monkeypatch.setattr(
        store, "reserve_action", lambda **_: {"verdict": "go", "count": 0, "cap": 0, "delay_sec": 0}
    )

    def rail():
        return RailClient(api_base=fake.base, api_key="k", web_base_url=fake.base)

    return make_ctx(
        TIER_ATTENDED, rail_factory=rail, config=Config(carousell_ai_web_base_url=fake.base)
    )


def _item(store, *, list_price=80.0):
    return store.create_item(
        title=f"Lamp {next(_TITLES)}", list_price=list_price, currency="SGD", size_bucket="m"
    )


def _publish(ctx, item) -> dict:
    return dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)


def _listing_calls(fake) -> list:
    return [c for c in fake.create_calls if c["tool"] == "create_listing"]


def _keys(fake) -> list:
    return [c["key"] for c in _listing_calls(fake)]


def _recorded(store, item) -> str:
    return store.get_item(item["id"])["listing_urls"].get("carousell-ai", "")


def test_a_lost_answer_gets_the_same_listing_back(ctx, store, fake) -> None:
    item = _item(store)
    fake.create_script = ["lost"]
    with pytest.raises(ToolError, match="publishing again within a day"):
        _publish(ctx, item)

    result = _publish(ctx, item)

    assert list(fake.listings) == ["L1"]
    assert result["url"] == f"{fake.base}/listing/L1" == _recorded(store, item)
    first, retry = _keys(fake)
    assert first and first == retry


def test_a_refusal_starts_a_new_attempt_under_a_new_key(ctx, store, fake) -> None:
    item = _item(store)
    fake.create_script = ["refused"]
    with pytest.raises(ToolError, match="title is required"):
        _publish(ctx, item)

    _publish(ctx, item)

    first, retry = _keys(fake)
    assert first != retry
    assert list(fake.listings) == ["L1"]


@pytest.mark.parametrize("step", ["busy", "http503"])
def test_an_answer_that_is_not_final_keeps_the_key(ctx, store, fake, step) -> None:
    item = _item(store)
    fake.create_script = [step]
    with pytest.raises(ToolError):
        _publish(ctx, item)

    _publish(ctx, item)

    first, retry = _keys(fake)
    assert first == retry
    assert list(fake.listings) == ["L1"]


def test_bazaars_internal_error_keeps_the_key(ctx, store, fake, monkeypatch) -> None:
    """bazaar answers "internal server error" for a fault, a recovered panic included, which may
    come after the listing was made."""
    item = _item(store)
    real = fake.create

    def fault(tool, args, key, step):
        if not fake.create_calls:
            real(tool, args, key, step)
            from tests.fake_carousell_ai_mcp import ToolFailure

            raise ToolFailure("internal server error")
        return real(tool, args, key, step)

    monkeypatch.setattr(fake, "create", fault)
    with pytest.raises(ToolError):
        _publish(ctx, item)

    _publish(ctx, item)

    first, retry = _keys(fake)
    assert first == retry
    assert list(fake.listings) == ["L1"]


def test_an_edit_after_a_lost_answer_still_gets_the_first_listing(ctx, store, fake) -> None:
    item = _item(store, list_price=80.0)
    fake.create_script = ["lost"]
    with pytest.raises(ToolError):
        _publish(ctx, item)
    store.update_item(item["id"], {"list_price": 95.0})

    result = _publish(ctx, item)

    assert list(fake.listings) == ["L1"]
    assert fake.listings["L1"]["price_cents"] == 8000
    assert _recorded(store, item) == f"{fake.base}/listing/L1"
    # Live at the old price, so the caller is told rather than handed a plain success.
    assert "does not show that edit" in result["note"]


def test_an_edit_after_a_failure_that_made_nothing_publishes_the_edit(ctx, store, fake) -> None:
    item = _item(store, list_price=80.0)
    fake.create_script = ["http503"]
    with pytest.raises(ToolError):
        _publish(ctx, item)
    store.update_item(item["id"], {"list_price": 95.0})

    result = _publish(ctx, item)

    assert [listing["price_cents"] for listing in fake.listings.values()] == [9500]
    assert "note" not in result


def test_a_page_not_live_yet_keeps_the_key(ctx, store, fake, monkeypatch) -> None:
    item = _item(store)
    real = RailClient.verify_listing_url
    fails = ["once"]

    def verify(self, url):
        if fails:
            fails.pop()
            raise RailToolError("listing page returned HTTP 404")
        real(self, url)

    monkeypatch.setattr(RailClient, "verify_listing_url", verify)
    with pytest.raises(ToolError):
        _publish(ctx, item)

    _publish(ctx, item)

    first, retry = _keys(fake)
    assert first == retry
    assert list(fake.listings) == ["L1"]


def test_a_crash_before_the_url_is_recorded_keeps_the_key(ctx, store, fake, monkeypatch) -> None:
    item = _item(store)
    real = store.record_listing_url

    def crash(*args, **kwargs):
        monkeypatch.setattr(store, "record_listing_url", real)
        raise StoreError("database is locked")

    monkeypatch.setattr(store, "record_listing_url", crash)
    with pytest.raises(ToolError):
        _publish(ctx, item)

    _publish(ctx, item)

    first, retry = _keys(fake)
    assert first == retry
    assert list(fake.listings) == ["L1"]


def test_a_publish_that_finished_meanwhile_is_not_repeated(ctx, store, fake, monkeypatch) -> None:
    """Two publishes of one item: the second passed the early check before the first recorded."""
    item = _item(store)
    url = f"{fake.base}/listing/L1"

    def first_one_finishes(**_):
        store.record_listing_url(item["id"], "carousell-ai", url)
        return {"verdict": "go", "count": 0, "cap": 0, "delay_sec": 0}

    monkeypatch.setattr(store, "reserve_action", first_one_finishes)

    result = _publish(ctx, item)

    assert result == {"listing_id": None, "url": url, "already_published": True}
    assert _listing_calls(fake) == []


def test_a_url_recorded_another_way_ends_the_publish_under_way(ctx, store, fake) -> None:
    item = _item(store)
    fake.create_script = ["http503"]
    with pytest.raises(ToolError):
        _publish(ctx, item)
    store.record_listing_url(item["id"], "carousell-ai", f"{fake.base}/listing/L9")
    store.archive_listing_url(item["id"], "carousell-ai")

    _publish(ctx, item)

    first, second = _keys(fake)
    assert first != second


def test_a_republish_after_the_listing_is_archived_is_a_new_attempt(ctx, store, fake) -> None:
    item = _item(store)
    _publish(ctx, item)
    store.archive_listing_url(item["id"], "carousell-ai")

    _publish(ctx, item)

    first, second = _keys(fake)
    assert first != second
    assert list(fake.listings) == ["L1", "L2"]


def test_a_checkout_link_after_a_lost_answer_is_the_same_link(ctx, store, fake) -> None:
    item = _item(store, list_price=100.0)
    store.record_listing_url(item["id"], "carousell-ai", f"{fake.base}/listing/L9")
    params = {"item_id": item["id"], "thread_id": "fb:1", "agreed_price": 100.0}
    fake.create_script = ["lost"]
    with pytest.raises(ToolError):
        dispatch("carousell_ai_create_checkout_link", params, ctx)

    result = dispatch("carousell_ai_create_checkout_link", params, ctx)

    assert len(fake.checkouts) == 1
    assert result["checkout_url"] == f"{fake.base}/checkout/c1"
    # The key is the sale's own id, so the retry needs nothing recorded before the first call.
    first, retry = (c["key"] for c in fake.create_calls)
    assert first == retry
    assert store.get_checkout(first)["checkout_url"] == result["checkout_url"]


_STEPS = st.lists(st.sampled_from(["ok", "lost", "http503", "refused", "busy", "edit"]), max_size=8)


@_PROPERTY
@given(steps=_STEPS)
def test_property_one_listing_under_one_key_per_attempt(ctx, store, fake, steps) -> None:
    """Property: for any sequence of lost answers, failures, refusals and edits, the item ends up
    with exactly one listing, every retry of one attempt sends the same key, and a new attempt
    after a refusal sends a new one."""
    fake.listings.clear()
    fake.keyed.clear()
    fake.create_calls.clear()
    fake.create_script.clear()
    item = _item(store)
    for step in steps:
        if _recorded(store, item):
            break
        if step == "edit":
            price = store.get_item(item["id"])["list_price"]
            store.update_item(item["id"], {"list_price": price + 1})
            continue
        fake.create_script.append(step)
        try:
            _publish(ctx, item)
        except ToolError:
            pass
        fake.create_script.clear()
        assert len(fake.listings) <= 1

    _publish(ctx, item)

    assert len(fake.listings) == 1
    assert _recorded(store, item) == f"{fake.base}/listing/{next(iter(fake.listings))}"
    calls = _listing_calls(fake)
    assert all(c["key"] for c in calls)
    for before, after in itertools.pairwise(calls):
        if before["outcome"] == "refused":
            assert after["key"] != before["key"]
        else:
            assert after["key"] == before["key"]
