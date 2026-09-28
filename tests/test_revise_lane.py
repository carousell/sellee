"""The revise lane: draining edits owed to browser marketplaces, one at a time, and telling the
seller how each one went from the row — never before it happened."""

from __future__ import annotations

import pytest
from tests.conftest import seed_setting
from tests.test_browser_editor import StubEditForm

from sellee import revise
from sellee.browser.client import BrowserUnavailable
from sellee.config import Config

FB_URL = "https://www.facebook.com/marketplace/item/123/"
CAROUSELL_URL = "https://www.carousell.sg/p/dyson-v8-456/"


@pytest.fixture(autouse=True)
def _no_settles(monkeypatch):
    """The lane drives with the real, jittered settles; a test has no page to wait for."""
    monkeypatch.setattr("sellee.browser.formfill.sleep", lambda _seconds: None)


def _listed(store, **kw) -> dict:
    base = {"title": "Dyson V8", "list_price": 120.0, "currency": "SGD"}
    base.update(kw)
    item = store.create_item(**base)
    store.record_listing_url(item["id"], "fb", FB_URL)
    store.record_listing_url(item["id"], "carousell", CAROUSELL_URL)
    return item


def _deps(store, bus, form=None, *, unavailable=False) -> revise.ReviseDeps:
    def factory():
        if unavailable:
            raise BrowserUnavailable("Chrome is not running")
        return form

    return revise.ReviseDeps(store=store, bus=bus, config=Config(), browser_factory=factory)


def _notices(store) -> list:
    return [n["text"] for n in store.list_queued_notices()]


# --- driven: Facebook ---------------------------------------------------------------------------


def test_a_driven_edit_lands_and_the_seller_is_told_after_it_did(store, bus) -> None:
    item = _listed(store)
    rev = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    form = StubEditForm()
    deps = _deps(store, bus, form)

    assert revise.run_next(deps) == rev
    assert form.saved["price"] == "120"
    assert store.get_listing_revision(rev)["status"] == "done"
    assert _notices(store) == []  # nothing said yet — reporting is the next tick's first act

    revise.revise_lane(deps)
    assert _notices(store) == [
        f"Dyson V8 on Facebook Marketplace now shows the new price: {FB_URL}"
    ]


def test_an_unverified_edit_is_retried_later_then_reported_as_failed(
    store, bus, monkeypatch
) -> None:
    item = _listed(store)
    rev = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    old_form = {"title": "Dyson V8", "price": "SGD150", "description": ""}
    deps = _deps(store, bus, StubEditForm(after_save=old_form))

    revise.run_next(deps)
    row = store.get_listing_revision(rev)
    assert row["status"] == "pending" and row["attempts"] == 1
    assert revise.run_next(deps) is None  # not due again yet: attempts are spaced

    monkeypatch.setattr(revise, "REVISE_RETRY_AFTER_SEC", 0.0)
    for _ in range(revise.REVISE_MAX_ATTEMPTS - 1):
        revise.run_next(deps)
    row = store.get_listing_revision(rev)
    assert row["status"] == "failed"
    assert row["attempts"] == revise.REVISE_MAX_ATTEMPTS

    revise.report_settled(deps)
    (text,) = _notices(store)
    assert text.startswith("I couldn't change the price on Dyson V8's Facebook Marketplace listing")
    assert FB_URL in text


def test_a_form_missing_a_field_is_retried_rather_than_failed(store, bus) -> None:
    # A field the form did not render this time may render next time; nothing was saved.
    item = _listed(store)
    rev = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    deps = _deps(store, bus, StubEditForm(marked=("title", "save")))
    revise.run_next(deps)
    row = store.get_listing_revision(rev)
    assert row["status"] == "pending" and row["attempts"] == 1


# --- the gates before a claim -------------------------------------------------------------------


def test_no_browser_costs_the_row_nothing(store, bus) -> None:
    item = _listed(store)
    rev = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    assert revise.run_next(_deps(store, bus, unavailable=True)) is None
    row = store.get_listing_revision(rev)
    assert row["status"] == "pending" and row["attempts"] == 0


def test_a_busy_browser_costs_the_row_nothing(store, bus) -> None:
    item = _listed(store)
    rev = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    store.hold_browser("signin", "the seller is signing in", 600)
    assert revise.run_next(_deps(store, bus, StubEditForm())) is None
    assert store.get_listing_revision(rev)["attempts"] == 0


def test_one_at_a_time(store, bus) -> None:
    item = _listed(store)
    store.queue_listing_revision(item["id"], "fb", ["list_price"])
    store.queue_listing_revision(item["id"], "carousell", ["list_price"])
    store.claim_listing_revision()  # something is already running
    assert revise.run_next(_deps(store, bus, StubEditForm())) is None


def test_a_paused_agent_reports_but_drives_nothing(store, bus) -> None:
    item = _listed(store)
    rev = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    store.set_paused(True)
    revise.revise_lane(_deps(store, bus, StubEditForm()))
    assert store.get_listing_revision(rev)["status"] == "pending"


# --- the checks at claim time -------------------------------------------------------------------


def test_an_item_that_sold_since_is_failed_with_the_reason(store, bus, monkeypatch) -> None:
    item = _listed(store)
    rev = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    monkeypatch.setattr(store, "sold_item_ids", lambda: {item["id"]})
    revise.run_next(_deps(store, bus, StubEditForm()))
    row = store.get_listing_revision(rev)
    assert row["status"] == "failed" and "sold" in row["last_error"]


def test_a_marketplace_disconnected_since_is_failed_not_driven(store, bus) -> None:
    item = _listed(store)
    rev = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    seed_setting(store, "connected_markets", ["carousell"])
    form = StubEditForm()
    revise.run_next(_deps(store, bus, form))
    assert store.get_listing_revision(rev)["status"] == "failed"
    assert form.saved is None


# --- recipe markets: a model pass ---------------------------------------------------------------


def test_a_recipe_market_becomes_an_edit_pass_bound_to_the_row(carousell_only, bus) -> None:
    store = carousell_only
    item = _listed(store)
    rev = store.queue_listing_revision(item["id"], "carousell", ["description"])
    revise.run_next(_deps(store, bus, StubEditForm()))

    row = store.get_listing_revision(rev)
    assert row["status"] == "running" and row["pass_id"]
    queued = store.get_pass(row["pass_id"])
    assert queued["type"] == "edit"
    assert queued["payload"] == {
        "revision_id": rev,
        "item_id": item["id"],
        "market": "carousell",
        "changed": ["description"],
    }


def test_a_pass_that_ended_without_recording_is_handed_back(carousell_only, bus) -> None:
    store = carousell_only
    item = _listed(store)
    rev = store.queue_listing_revision(item["id"], "carousell", ["description"])
    deps = _deps(store, bus, StubEditForm())
    revise.run_next(deps)
    pass_id = store.get_listing_revision(rev)["pass_id"]

    assert revise.settle_orphans(deps) == 0  # still queued: leave it alone
    store.finish_pass(pass_id, status="done", cls="ok")
    assert revise.settle_orphans(deps) == 1
    row = store.get_listing_revision(rev)
    assert row["status"] == "pending" and "without saying" in row["last_error"]


def test_a_drive_a_crash_left_running_is_swept(store, bus, monkeypatch) -> None:
    item = _listed(store)
    rev = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    store.claim_listing_revision()
    deps = _deps(store, bus, StubEditForm())
    assert revise.settle_orphans(deps) == 0  # just claimed — not stale
    deps.now = lambda: 10**12
    assert revise.settle_orphans(deps) == 1
    assert store.get_listing_revision(rev)["status"] == "pending"


# --- the words ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changed,phrase",
    [
        (["list_price"], "price"),
        (["description", "list_price"], "description and price"),
        (["description", "list_price", "title"], "description, price and title"),
    ],
)
def test_fields_read_as_words(changed, phrase) -> None:
    assert revise.fields_phrase(changed) == phrase


def test_no_notice_implies_a_quick_turnaround() -> None:
    from tests.guard.test_channel_copy_never_implies_speed import BANNED

    for template in (revise.REVISED_NOTICE, revise.FAILED_NOTICE):
        assert not any(phrase in template.lower() for phrase in BANNED)


def test_a_result_overtaken_by_a_newer_edit_is_closed_silently(store, bus) -> None:
    # The seller changed the price again while the first edit was being driven. The first one's
    # "now shows the new price" would describe a number that is no longer the ask; the newer row
    # speaks for both when it settles.
    item = _listed(store)
    first = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    store.claim_listing_revision()
    store.queue_listing_revision(item["id"], "fb", ["list_price"])  # a new pending row
    store.finish_listing_revision(first, status="done")

    deps = _deps(store, bus, StubEditForm())
    assert revise.report_settled(deps) == 0
    assert _notices(store) == []
    assert store.unreported_listing_revisions() == []


def test_chrome_vanishing_mid_edit_spends_the_attempt(store, bus) -> None:
    # Review: the attempt was handed back uncounted, so a Chrome that kept dying retried the edit
    # every interval forever and the seller was never told.
    item = _listed(store)
    rev = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] % 2 == 0:  # up for the pre-claim check, gone when the drive starts
            raise BrowserUnavailable("Chrome closed")
        return StubEditForm()

    deps = revise.ReviseDeps(store=store, bus=bus, config=Config(), browser_factory=flaky)
    revise.run_next(deps)
    assert store.get_listing_revision(rev)["attempts"] == 1
    assert store.get_listing_revision(rev)["status"] == "pending"


def test_chrome_vanishing_every_time_ends_in_a_reported_failure(store, bus, monkeypatch) -> None:
    item = _listed(store)
    rev = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            raise BrowserUnavailable("Chrome closed")
        return StubEditForm()

    monkeypatch.setattr(revise, "REVISE_RETRY_AFTER_SEC", 0.0)
    deps = revise.ReviseDeps(store=store, bus=bus, config=Config(), browser_factory=flaky)
    for _ in range(revise.REVISE_MAX_ATTEMPTS + 2):
        revise.run_next(deps)
    assert store.get_listing_revision(rev)["status"] == "failed"
    revise.report_settled(deps)
    assert any("couldn't change the price" in text for text in _notices(store))


def test_a_result_the_newer_edit_does_not_cover_is_still_reported(store, bus) -> None:
    # Review: a newer price edit silently swallowed the failure of a description edit.
    item = _listed(store)
    first = store.queue_listing_revision(item["id"], "fb", ["description"])
    store.claim_listing_revision()
    store.finish_listing_revision(first, status="failed", error="the form refused it")
    newer = store.queue_listing_revision(item["id"], "fb", ["list_price"])  # after it settled
    assert store.get_listing_revision(newer)["changed"] == ["list_price"]

    revise.report_settled(_deps(store, bus, StubEditForm()))
    (text,) = _notices(store)
    assert "couldn't change the description" in text
