"""The store half of changing a live listing: the floor clamp that keeps `floor <= list_price`
true on every price write, the money validation `update_item` never had, the narrower
`revise_item` door, and the revision queue the browser lane drains."""

from __future__ import annotations

import pytest

from sellee.store import ItemNotFound, Store, StoreError


def _item(store: Store, **kw) -> dict:
    base = {"title": "Dyson V8", "list_price": 150.0, "currency": "SGD"}
    base.update(kw)
    return store.create_item(**base)


# --- the floor clamp ----------------------------------------------------------------------------


def test_lowering_the_price_under_the_floor_moves_the_floor_down_with_it(store: Store) -> None:
    item = _item(store)
    store.set_floor(item["id"], 130.0, "seller")
    store.update_item(item["id"], {"list_price": 120.0})
    floor = store.get_floor(item["id"])
    assert floor["floor"] == 120.0
    # still the seller's floor: the clamp moves the number, never the provenance
    assert floor["source"] == "seller"


def test_a_price_still_above_the_floor_leaves_the_floor_alone(store: Store) -> None:
    item = _item(store)
    store.set_floor(item["id"], 100.0, "seller")
    store.update_item(item["id"], {"list_price": 120.0})
    assert store.get_floor(item["id"])["floor"] == 100.0


def test_raising_the_price_never_raises_the_floor(store: Store) -> None:
    item = _item(store)
    store.set_floor(item["id"], 100.0, "seller")
    store.update_item(item["id"], {"list_price": 200.0})
    assert store.get_floor(item["id"])["floor"] == 100.0


def test_no_floor_means_nothing_to_clamp(store: Store) -> None:
    item = _item(store)
    store.update_item(item["id"], {"list_price": 10.0})
    assert store.get_floor(item["id"]) is None


def test_the_clamp_applies_to_a_default_floor_too(store: Store) -> None:
    item = _item(store)
    store.set_floor(item["id"], 150.0, "default")
    store.update_item(item["id"], {"list_price": 90.0})
    assert store.get_floor(item["id"])["floor"] == 90.0


# --- money validation ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -5, "eighty", float("inf"), float("nan"), True])
def test_update_item_refuses_a_price_that_is_not_one(store: Store, bad) -> None:
    item = _item(store)
    with pytest.raises(StoreError):
        store.update_item(item["id"], {"list_price": bad})
    assert store.get_item(item["id"])["list_price"] == 150.0


# --- revise_item --------------------------------------------------------------------------------


def test_revise_item_reports_the_clamp_as_a_bare_boolean(store: Store) -> None:
    item = _item(store)
    store.set_floor(item["id"], 140.0, "seller")
    result = store.revise_item(item["id"], {"list_price": 120.0})
    assert result["floor_clamped"] is True
    assert result["item"]["list_price"] == 120.0
    # structurally value-free: nothing in the ack carries the floor
    assert set(result) == {"item", "floor_clamped"}
    assert "floor" not in result["item"]


def test_revise_item_without_a_price_change_clamps_nothing(store: Store) -> None:
    item = _item(store)
    store.set_floor(item["id"], 140.0, "seller")
    result = store.revise_item(item["id"], {"description": "comes with the original box"})
    assert result["floor_clamped"] is False
    assert result["item"]["description"] == "comes with the original box"


@pytest.mark.parametrize("field", ["currency", "condition", "status", "size_bucket"])
def test_revise_item_refuses_what_a_live_listing_does_not_show_or_cannot_take(
    store: Store, field
) -> None:
    item = _item(store)
    value = {"currency": "USD", "condition": "like new", "status": "ready", "size_bucket": "s"}
    with pytest.raises(StoreError, match="non-writable"):
        store.revise_item(item["id"], {field: value[field]})


def test_revise_item_missing_item(store: Store) -> None:
    with pytest.raises(ItemNotFound):
        store.revise_item("item_nope", {"title": "x"})


# --- the revision queue -------------------------------------------------------------------------


def test_queue_then_claim_then_finish(store: Store) -> None:
    item = _item(store)
    rev = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    assert not store.revision_in_flight()

    claimed = store.claim_listing_revision()
    assert claimed["revision_id"] == rev
    assert claimed["status"] == "running"
    assert claimed["attempts"] == 1
    assert claimed["changed"] == ["list_price"]
    assert store.revision_in_flight()
    assert store.claim_listing_revision() is None  # single-flight: nothing else is pending

    done = store.finish_listing_revision(rev, status="done", accepted={"price": 120})
    assert done["status"] == "done"
    assert done["accepted"] == {"price": 120}
    assert not store.revision_in_flight()


def test_a_second_edit_supersedes_the_pending_one_and_carries_its_fields(store: Store) -> None:
    item = _item(store)
    first = store.queue_listing_revision(item["id"], "fb", ["description"])
    second = store.queue_listing_revision(item["id"], "fb", ["list_price"])

    rows = {r["revision_id"]: r for r in store.list_listing_revisions()}
    assert rows[first]["status"] == "superseded"
    assert rows[second]["status"] == "pending"
    # the description edit is still owed on fb — the newer row must not drop it
    assert rows[second]["changed"] == ["description", "list_price"]


def test_supersede_is_per_market(store: Store) -> None:
    item = _item(store)
    store.queue_listing_revision(item["id"], "fb", ["list_price"])
    store.queue_listing_revision(item["id"], "carousell", ["list_price"])
    assert len(store.list_listing_revisions("pending")) == 2


def test_a_running_revision_is_not_superseded(store: Store) -> None:
    item = _item(store)
    first = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    store.claim_listing_revision()
    store.queue_listing_revision(item["id"], "fb", ["title"])
    rows = {r["revision_id"]: r for r in store.list_listing_revisions()}
    assert rows[first]["status"] == "running"


def test_retry_hands_the_row_back_with_the_attempt_spent(store: Store) -> None:
    item = _item(store)
    rev = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    store.claim_listing_revision()
    back = store.finish_listing_revision(rev, status="pending", retry=True, error="busy")
    assert back["status"] == "pending"
    assert back["last_error"] == "busy"
    assert store.claim_listing_revision()["attempts"] == 2


def test_finish_refuses_a_non_terminal_status(store: Store) -> None:
    item = _item(store)
    rev = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    store.claim_listing_revision()
    with pytest.raises(StoreError):
        store.finish_listing_revision(rev, status="pending")


def test_queue_needs_a_field_and_an_item(store: Store) -> None:
    item = _item(store)
    with pytest.raises(StoreError):
        store.queue_listing_revision(item["id"], "fb", [])
    with pytest.raises(ItemNotFound):
        store.queue_listing_revision("item_nope", "fb", ["title"])


def test_reporting_is_exactly_once_and_superseded_rows_owe_nothing(store: Store) -> None:
    item = _item(store)
    store.queue_listing_revision(item["id"], "fb", ["description"])
    rev = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    store.claim_listing_revision()
    store.finish_listing_revision(rev, status="done", accepted={"price": 120})

    owed = store.unreported_listing_revisions()
    assert [r["revision_id"] for r in owed] == [rev]  # the superseded row is closed silently

    assert store.report_listing_revision(rev, "Dyson V8 now $120 on Facebook", ref=item["id"])
    assert not store.report_listing_revision(rev, "again", ref=item["id"])
    assert store.unreported_listing_revisions() == []

    text = "Dyson V8 now $120 on Facebook"
    notices = [n for n in store.list_queued_notices() if n["text"] == text]
    assert len(notices) == 1
    assert notices[0]["ref"] == item["id"]
    assert notices[0]["pass_id"] is None


def test_a_silent_report_closes_the_row_without_a_notice(store: Store) -> None:
    item = _item(store)
    rev = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    store.claim_listing_revision()
    store.finish_listing_revision(rev, status="failed", error="nope")
    before = len(store.list_queued_notices())
    assert store.report_listing_revision(rev, None)
    assert len(store.list_queued_notices()) == before


# --- update_item on a listed item ---------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"list_price": 99.0},
        {"title": "New"},
        {"description": "x"},
        {"currency": "USD"},
        {"condition": "used"},
    ],
)
def test_update_item_refuses_what_a_buyer_sees_once_listed(store: Store, fields) -> None:
    # Review: only a prompt line kept the model from changing a live price on the record alone.
    item = _item(store)
    store.record_listing_url(item["id"], "fb", "https://www.facebook.com/marketplace/item/1/")
    with pytest.raises(StoreError, match="update_live_listing"):
        store.update_item(item["id"], fields)
    assert store.get_item(item["id"])["list_price"] == 150.0


def test_update_item_still_writes_bookkeeping_on_a_listed_item(store: Store) -> None:
    item = _item(store)
    store.record_listing_url(item["id"], "fb", "https://www.facebook.com/marketplace/item/1/")
    assert store.update_item(item["id"], {"status": "ready"})["status"] == "ready"


def test_update_item_writes_freely_before_anything_is_listed(store: Store) -> None:
    item = _item(store)
    assert store.update_item(item["id"], {"list_price": 99.0})["list_price"] == 99.0


def test_a_new_edit_carries_the_fields_of_one_still_running(store: Store) -> None:
    # Review: a running description edit that later failed was covered by nothing — the newer
    # price edit neither pushed the description nor let the failure be reported.
    item = _item(store)
    first = store.queue_listing_revision(item["id"], "fb", ["description"])
    store.claim_listing_revision()
    second = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    rows = {r["revision_id"]: r for r in store.list_listing_revisions()}
    assert rows[first]["status"] == "running"  # mid-drive, so not superseded
    assert rows[second]["changed"] == ["description", "list_price"]
