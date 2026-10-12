"""The edit pass type: what it refuses at claim, what its prompt hands the model, and that it gets
the browser only where a recipe says what to do with it."""

from __future__ import annotations

import pytest
from tests.conftest import seed_setting

from sellee import passes

CAROUSELL_URL = "https://www.carousell.sg/p/dyson-v8-456/"


def _running(store, market="carousell", changed=("description",)) -> dict:
    item = store.create_item(title="Dyson V8", list_price=120.0, currency="SGD")
    store.record_listing_url(item["id"], market, CAROUSELL_URL)
    rev = store.queue_listing_revision(item["id"], market, list(changed))
    store.claim_listing_revision()
    return {"revision_id": rev, "item_id": item["id"], "market": market, "changed": list(changed)}


def test_a_live_edit_payload_passes(store) -> None:
    passes.validate_payload("edit", _running(store), store)


@pytest.mark.parametrize("missing", ["revision_id", "item_id", "market"])
def test_a_payload_missing_an_id_is_refused(store, missing) -> None:
    payload = dict(_running(store))
    payload.pop(missing)
    with pytest.raises(passes.PassPayloadError, match=missing):
        passes.validate_payload("edit", payload, store)


def test_a_disconnected_market_is_refused_at_claim(store) -> None:
    payload = _running(store)
    seed_setting(store, "connected_markets", ["fb"])
    with pytest.raises(passes.PassPayloadError, match="connected"):
        passes.validate_payload("edit", payload, store)


def test_an_edit_the_driver_can_make_is_never_a_pass(store) -> None:
    payload = dict(_running(store), market="fb", changed=["list_price"])
    with pytest.raises(passes.PassPayloadError, match="driving its form"):
        passes.validate_payload("edit", payload, store)


def test_an_edit_no_longer_in_progress_is_refused(store) -> None:
    payload = _running(store)
    store.finish_listing_revision(payload["revision_id"], status="failed")
    with pytest.raises(passes.PassPayloadError, match="no longer in progress"):
        passes.validate_payload("edit", payload, store)


def test_the_prompt_names_the_listing_the_fields_and_the_id_to_report(store) -> None:
    payload = _running(store)
    prompt = passes.PASS_TYPES["edit"].build_prompt(payload, store, "pass_1")
    assert CAROUSELL_URL in prompt
    assert "description" in prompt
    assert payload["revision_id"] in prompt
    assert "record_listing_revision" in prompt


def test_the_pass_gets_the_recipe_and_the_browser(store) -> None:
    payload = _running(store)
    spec = passes.PASS_TYPES["edit"]
    assert spec.skills_for(payload, store, "p") == ("sellee-conventions", "edit-flow-carousell")
    assert spec.build_browser_tools(payload, store, "p") == passes.PUBLISH_BROWSER_TOOLS


def test_progress_means_the_edit_was_recorded(store) -> None:
    payload = _running(store)
    progressed = passes.PASS_TYPES["edit"].made_progress
    assert progressed(payload, store, "p") is False
    store.finish_listing_revision(payload["revision_id"], status="done")
    assert progressed(payload, store, "p") is True
