"""The revise lane: draining edits owed to browser marketplaces, one at a time, and telling the
seller how each one went from the row — never before it happened."""

from __future__ import annotations

import time

import pytest
from tests.conftest import seed_setting
from tests.test_browser_editor import StubEditForm

from sellee import daemon, revise
from sellee.browser.client import BrowserError, BrowserUnavailable
from sellee.browser.sink import BrowserReplySink
from sellee.channel import activity
from sellee.config import Config
from sellee.rail.registration_sink import RegistrationReplySink
from sellee.tools import revise as revise_tool
from sellee.tools.registry import TIER_PASS_CHANNEL, TIER_PASS_EDIT, ToolError, dispatch

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


def _seller_hears(store) -> list:
    """Every notice queued for the seller, then the activity update their untold tells make."""
    update = activity.untold_text(store)
    return _notices(store) + ([update] if update else [])


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


class _NoRoom:
    """A governor whose page loads for the market are spent."""

    def can_start(self, market, loads=1):
        return False


def test_an_edit_held_for_page_loads_says_so_once(store, bus) -> None:
    # Live run 4: a price edit waited half an hour with nothing to show why.
    item = _listed(store)
    store.queue_listing_revision(item["id"], "fb", ["list_price"])
    deps = _deps(store, bus, StubEditForm())
    deps.governor = _NoRoom()
    held = []
    bus.subscribe(lambda event: held.append(event) if event.kind == "revise.held" else None)

    revise.run_next(deps)
    revise.run_next(deps)

    assert [event.payload["reason"] for event in held] == ["page_loads"]


def test_an_edit_with_no_page_loads_left_is_not_claimed(store, bus) -> None:
    item = _listed(store)
    rev = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    form = StubEditForm()
    deps = _deps(store, bus, form)
    deps.governor = _NoRoom()

    assert revise.run_next(deps) is None

    row = store.get_listing_revision(rev)
    assert row["status"] == "pending" and row["attempts"] == 0
    assert form.actions == []


def test_a_facebook_edit_waits_out_the_quiet_hours(store, bus, monkeypatch) -> None:
    from sellee.browser import governor

    item = _listed(store)
    rev = store.queue_listing_revision(item["id"], "fb", ["list_price"])
    monkeypatch.setattr(governor, "in_quiet_hours", lambda store, config, now: True)
    form = StubEditForm()

    assert revise.run_next(_deps(store, bus, form)) is None

    assert store.get_listing_revision(rev)["attempts"] == 0
    assert form.actions == []


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


# --- the buyers who asked hear once the marketplace shows the change -----------------------------

CRAIGSLIST_URL = "https://www.craigslist.org/view/d/dyson-v8/7978162581"
_FAST = Config(reply_delay_sec=(0, 0), interactive_reply_delay_sec=(0, 0))
_DROP = "The price of the Dyson V8 is now $80 (was $120)."
_TOLD_1 = "📣 Dyson V8 — let 1 buyer who asked know about the price drop."
_TOLD_2 = "📣 Dyson V8 — let 2 buyers who asked know about the price drop."
_URLS = {"fb": FB_URL, "carousell": CAROUSELL_URL, "craigslist": CRAIGSLIST_URL}
_SINK = {"fb": "browser", "carousell": "browser", "craigslist": "registration"}


@pytest.fixture
def sent(monkeypatch, store):
    """What each marketplace's own reply sink was asked to send, as (sink, thread, text, kind)."""
    calls: list = []

    def recorder(name):
        def send(self, thread, text, kind, intent_id):
            calls.append((name, thread["thread_id"], text, kind))
            return {}

        return send

    monkeypatch.setattr(BrowserReplySink, "send", recorder("browser"))
    monkeypatch.setattr(RegistrationReplySink, "send", recorder("registration"))
    seed_setting(store, "connected_markets", ["carousell", "fb", "craigslist"])
    return calls


def _item_on(store, *markets) -> dict:
    item = store.create_item(
        title="Dyson V8", list_price=120.0, currency="SGD", description="Works."
    )
    for market in markets:
        store.record_listing_url(item["id"], market, _URLS[market])
    return item


def _asked(store, item_id: str, market: str, n: int) -> str:
    """A buyer on `market` who asked about the item and was answered."""
    thread_id = f"{market}:t{n}"
    store.create_thread(
        thread_id=thread_id, side="sell", market=market, counterpart_handle=f"b{n}", item_id=item_id
    )
    store.append_thread_message(thread_id, msg_id="q", direction="in", text="Available?", ts=1.0)
    store.mark_relay_answered(thread_id, "q", 1.0)
    return thread_id


def _asked_everywhere(store, item_id: str) -> None:
    for n, market in enumerate(("fb", "fb", "carousell", "carousell", "craigslist", "craigslist")):
        _asked(store, item_id, market, n)


def _lane(store, bus, make_ctx, form=None, cfg=_FAST) -> revise.ReviseDeps:
    """The lane as the daemon wires it: an edit session sending through `reply_sink_for`."""

    def context_factory(session):
        ctx = make_ctx(session.tier, pass_id=session.pass_id, config=cfg)
        ctx.reply_sink = lambda market: daemon.reply_sink_for(
            market, store=store, bus=bus, cfg=cfg, rail_factory=object, browser_factory=object
        )
        return ctx

    deps = _deps(store, bus, form)
    deps.send_updates = daemon.past_enquirer_sender(context_factory)
    return deps


def _drop_to_80(make_ctx, item_id: str) -> dict:
    ctx = make_ctx(TIER_PASS_CHANNEL, pass_id="p1")
    params = {"item_id": item_id, "fields": {"list_price": 80}}
    return dispatch("update_live_listing", params, ctx)


def _settle(store, rev: str, status: str, accepted=None) -> None:
    claimed = store.claim_listing_revision()
    assert claimed["revision_id"] == rev
    store.finish_listing_revision(rev, status=status, accepted=accepted)


def _told(sent) -> list:
    return sorted((sink, thread_id) for sink, thread_id, _text, _kind in sent)


def test_a_facebook_price_drop_waits_for_its_revision_then_tells_its_buyers(
    store, bus, make_ctx, sent
) -> None:
    item = _item_on(store, "fb")
    _asked_everywhere(store, item["id"])
    assert _drop_to_80(make_ctx, item["id"])["markets"]["fb"]["status"] == "queued"
    form = StubEditForm(values={"title": "Dyson V8", "price": "120", "description": "Works."})
    deps = _lane(store, bus, make_ctx, form)

    revise.revise_lane(deps)  # drives the edit; nothing is told before it settles
    assert store.list_listing_revisions("done") and sent == []

    revise.revise_lane(deps)
    assert sent == [
        ("browser", "fb:t0", _DROP, "followup"),
        ("browser", "fb:t1", _DROP, "followup"),
    ]
    assert _TOLD_2 in _seller_hears(store)
    assert store.get_thread("fb:t0")["last_followup_ts"] is not None

    revise.revise_lane(deps)
    assert len(sent) == 2


def test_a_carousell_edit_pass_recording_done_tells_its_buyers(store, bus, make_ctx, sent) -> None:
    item = _item_on(store, "carousell")
    _asked_everywhere(store, item["id"])
    _drop_to_80(make_ctx, item["id"])
    deps = _lane(store, bus, make_ctx)
    revise.run_next(deps)
    (row,) = store.list_listing_revisions("running")
    pass_ctx = make_ctx(TIER_PASS_EDIT, pass_id=row["pass_id"])
    params = {"revision_id": row["revision_id"], "outcome": "done", "shown": {"price": "80.00"}}

    dispatch("record_listing_revision", params, pass_ctx)
    revise.revise_lane(deps)
    assert sent == []  # the edit pass still holds the browser

    store.finish_pass(row["pass_id"], status="done", cls="ok")
    revise.revise_lane(deps)
    assert _told(sent) == [("browser", "carousell:t2"), ("browser", "carousell:t3")]
    assert {text for _sink, _t, text, _k in sent} == {_DROP}
    assert _TOLD_2 in _seller_hears(store)

    with pytest.raises(ToolError):  # a second record is refused, and the lane tells nobody again
        dispatch("record_listing_revision", params, pass_ctx)
    revise.revise_lane(deps)
    assert len(sent) == 2


def test_craigslist_buyers_hear_through_the_registration_address(store, bus, make_ctx, sent):
    item = _item_on(store, "craigslist")
    _asked_everywhere(store, item["id"])
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})

    revise.tell_settled(_lane(store, bus, make_ctx))
    assert sent == [
        ("registration", "craigslist:t4", _DROP, "followup"),
        ("registration", "craigslist:t5", _DROP, "followup"),
    ]
    assert _TOLD_2 in _seller_hears(store)


@pytest.mark.parametrize("market", ["fb", "carousell", "craigslist"])
@pytest.mark.parametrize("status", ["done", "failed"])
@pytest.mark.parametrize("shown", ["80", "120", None])
def test_only_a_done_revision_showing_the_new_price_tells_and_only_once(
    store, bus, make_ctx, sent, market, status, shown
) -> None:
    item = _item_on(store, market)
    _asked_everywhere(store, item["id"])
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], status, {"price": shown} if shown else {})
    deps = _lane(store, bus, make_ctx)

    revise.tell_settled(deps)
    told = _told(sent)
    if status == "done" and shown == "80":
        assert told == [(_SINK[market], f"{market}:t{n}") for n in _THREADS[market]]
    else:
        assert told == []
    assert store.listing_revisions_owing_buyers() == []

    # A settle repeated by a retry or the orphan sweep changes nothing, and tells nobody again.
    store.finish_listing_revision(row["revision_id"], status="done", accepted={"price": "80"})
    revise.settle_orphans(deps)
    revise.tell_settled(deps)
    assert _told(sent) == told


_THREADS = {"fb": (0, 1), "carousell": (2, 3), "craigslist": (4, 5)}


def test_quiet_hours_hold_a_browser_update_until_they_end(store, bus, make_ctx, sent) -> None:
    item = _item_on(store, "fb")
    _asked(store, item["id"], "fb", 0)
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})
    deps = _lane(store, bus, make_ctx)
    seed_setting(store, "quiet_hours", [0, 2359])  # HHMM: all day

    revise.tell_settled(deps)
    assert sent == []
    assert [r["revision_id"] for r in store.listing_revisions_owing_buyers()] == [
        row["revision_id"]
    ]

    seed_setting(store, "quiet_hours", [0, 0])
    revise.tell_settled(deps)
    assert _told(sent) == [("browser", "fb:t0")]
    assert _TOLD_1 in _seller_hears(store)


def test_an_update_held_past_its_window_is_dropped(store, bus, make_ctx, sent) -> None:
    item = _item_on(store, "fb")
    _asked(store, item["id"], "fb", 0)
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})
    deps = _lane(store, bus, make_ctx)
    deps.now = lambda: time.time() + revise.BUYER_UPDATE_WINDOW_SEC + 60

    revise.tell_settled(deps)
    assert sent == [] and store.listing_revisions_owing_buyers() == []


def test_a_price_that_moved_again_since_tells_nothing_stale(store, bus, make_ctx, sent) -> None:
    item = _item_on(store, "craigslist")
    _asked(store, item["id"], "craigslist", 0)
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})
    store.revise_item(item["id"], {"list_price": 90.0})  # the item no longer says 80

    revise.tell_settled(_lane(store, bus, make_ctx))
    assert sent == [] and store.listing_revisions_owing_buyers() == []


def test_a_sold_item_tells_nobody(store, bus, make_ctx, sent, monkeypatch) -> None:
    item = _item_on(store, "craigslist")
    _asked(store, item["id"], "craigslist", 0)
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})
    monkeypatch.setattr(store, "sold_item_ids", lambda: {item["id"]})

    revise.tell_settled(_lane(store, bus, make_ctx))
    assert sent == []


def test_a_paused_agent_tells_nobody_and_keeps_it_owed(store, bus, make_ctx, sent) -> None:
    item = _item_on(store, "craigslist")
    _asked(store, item["id"], "craigslist", 0)
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})
    store.set_paused(True)

    revise.revise_lane(_lane(store, bus, make_ctx))
    assert sent == [] and store.listing_revisions_owing_buyers()


def test_buyers_held_by_the_pacing_cap_are_told_later_and_counted_once(
    store, bus, make_ctx, sent
) -> None:
    item = _item_on(store, "fb")
    _asked(store, item["id"], "fb", 0)
    _asked(store, item["id"], "fb", 1)
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})
    deps = _capped_lane(store, bus, make_ctx)

    assert revise.tell_settled(deps) == 1  # the second waits for the hour's slot
    assert store.listing_revisions_owing_buyers()
    assert [text for text in _seller_hears(store) if text.startswith("📣")] == [_TOLD_1]

    with store._db.transaction() as conn:  # noqa: SLF001 — the hour has passed
        conn.execute("DELETE FROM pacing_actions")
    assert revise.tell_settled(deps) == 1
    assert _told(sent) == [("browser", "fb:t0"), ("browser", "fb:t1")]
    assert [text for text in _seller_hears(store) if text.startswith("📣")] == [_TOLD_2]


def _capped_lane(store, bus, make_ctx) -> revise.ReviseDeps:
    """The lane with room for one browser action an hour."""
    return _lane(store, bus, make_ctx, cfg=Config(reply_delay_sec=(0, 0), max_actions_per_hour=1))


def _hour_passes(store) -> None:
    with store._db.transaction() as conn:  # noqa: SLF001 — the pacing window moves on
        conn.execute("DELETE FROM pacing_actions")


def test_a_buyer_told_while_others_waited_is_not_told_again_after_writing(
    store, bus, make_ctx, sent
) -> None:
    # Review: a held round re-planned every thread, so a told buyer who wrote again heard it twice.
    item = _item_on(store, "fb")
    _asked(store, item["id"], "fb", 0)
    _asked(store, item["id"], "fb", 1)
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})
    deps = _capped_lane(store, bus, make_ctx)

    assert revise.tell_settled(deps) == 1
    (first,) = sent
    later = time.time() + 5
    store.append_thread_message(first[1], msg_id="q2", direction="in", text="60?", ts=later)
    store.mark_relay_answered(first[1], "q2", later)
    _hour_passes(store)

    assert revise.tell_settled(deps) == 1
    assert _told(sent) == [("browser", "fb:t0"), ("browser", "fb:t1")]
    assert store.listing_revisions_owing_buyers() == []


def test_a_send_that_failed_before_reaching_the_buyer_is_tried_again(
    store, bus, make_ctx, sent, monkeypatch
) -> None:
    # Review: a send that failed before the buyer saw it closed the row, so they were never told.
    item = _item_on(store, "fb")
    _asked(store, item["id"], "fb", 0)
    _asked(store, item["id"], "fb", 1)  # sent after t0 failed, in the same round
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})
    deps = _lane(store, bus, make_ctx)
    recorded = BrowserReplySink.send

    def fails_once(self, thread, text, kind, intent_id):
        monkeypatch.setattr(BrowserReplySink, "send", recorded)
        raise BrowserError("the composer was not there")

    monkeypatch.setattr(BrowserReplySink, "send", fails_once)
    assert revise.tell_settled(deps) == 1
    assert store.listing_revisions_owing_buyers()

    assert revise.tell_settled(deps) == 1
    assert _told(sent) == [("browser", "fb:t0"), ("browser", "fb:t1")]
    assert _TOLD_2 in _seller_hears(store)


def test_the_sellers_tell_is_tied_to_the_item_and_written_once(store, bus, make_ctx, sent):
    item = _item_on(store, "craigslist")
    _asked(store, item["id"], "craigslist", 0)
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})
    deps = _lane(store, bus, make_ctx)

    revise.tell_settled(deps)
    revise.tell_settled(deps)
    (tell,) = [t for t in store.untold_tells() if t["kind"] == "buyers_updated"]
    assert tell["item_id"] == item["id"]
    told = [e.payload for e in bus.store.read(kinds=["past_enquirers.told"])]
    assert {"item_id": item["id"], "market": "craigslist", "told": 1} in told
    assert len(told) == 1


def test_quiet_hours_hold_only_the_browser_markets(store, bus, make_ctx, sent) -> None:
    item = _item_on(store, "fb", "craigslist")
    _asked(store, item["id"], "fb", 0)
    _asked(store, item["id"], "craigslist", 1)
    _drop_to_80(make_ctx, item["id"])
    for _ in range(2):
        claimed = store.claim_listing_revision()
        store.finish_listing_revision(
            claimed["revision_id"], status="done", accepted={"price": "80"}
        )
    with store._db.transaction() as conn:  # noqa: SLF001 — Facebook settled first, so is met first
        conn.execute(
            "UPDATE listing_revisions SET finished_ts = finished_ts - 1 WHERE market = 'fb'"
        )
    seed_setting(store, "quiet_hours", [0, 2359])  # HHMM: all day

    revise.tell_settled(_lane(store, bus, make_ctx))
    assert _told(sent) == [("registration", "craigslist:t1")]
    assert [r["market"] for r in store.listing_revisions_owing_buyers()] == ["fb"]


def test_buyers_told_before_the_window_closed_are_still_counted(store, bus, make_ctx, sent):
    item = _item_on(store, "fb")
    _asked(store, item["id"], "fb", 0)
    _asked(store, item["id"], "fb", 1)
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})
    deps = _capped_lane(store, bus, make_ctx)
    assert revise.tell_settled(deps) == 1

    deps.now = lambda: time.time() + revise.BUYER_UPDATE_WINDOW_SEC + 60
    revise.tell_settled(deps)
    assert len(sent) == 1 and store.listing_revisions_owing_buyers() == []
    assert _TOLD_1 in _seller_hears(store)


@pytest.mark.parametrize("moves", ["price", "sold"])
def test_an_item_that_moved_after_some_were_told_closes_with_only_their_tell(
    store, bus, make_ctx, sent, monkeypatch, moves
) -> None:
    item = _item_on(store, "fb")
    _asked(store, item["id"], "fb", 0)
    _asked(store, item["id"], "fb", 1)
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})
    deps = _capped_lane(store, bus, make_ctx)
    assert revise.tell_settled(deps) == 1
    if moves == "price":
        store.revise_item(item["id"], {"list_price": 90.0})
    else:
        monkeypatch.setattr(store, "sold_item_ids", lambda: {item["id"]})

    revise.tell_settled(deps)
    assert len(sent) == 1 and store.listing_revisions_owing_buyers() == []
    # The buyer already told is still counted; nobody else is told of the stale change.
    assert [text for text in _seller_hears(store) if text.startswith("📣")] == [_TOLD_1]


@pytest.mark.parametrize("told_first", [0, 1], ids=["held", "partly-told"])
def test_a_listing_taken_down_while_updates_wait_tells_nobody_more(
    store, bus, make_ctx, sent, told_first
) -> None:
    item = _item_on(store, "fb")
    _asked(store, item["id"], "fb", 0)
    _asked(store, item["id"], "fb", 1)
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})
    deps = _capped_lane(store, bus, make_ctx)
    if told_first:
        assert revise.tell_settled(deps) == 1
    else:
        seed_setting(store, "quiet_hours", [0, 2359])  # HHMM: all day
        assert revise.tell_settled(deps) == 0
        seed_setting(store, "quiet_hours", [0, 0])

    store.archive_listing_url(item["id"], "fb")
    _hour_passes(store)
    revise.tell_settled(deps)

    assert len(sent) == told_first and store.listing_revisions_owing_buyers() == []


def test_a_lane_wired_to_send_nothing_tells_nobody(store, bus, make_ctx, sent) -> None:
    item = _item_on(store, "craigslist")
    _asked(store, item["id"], "craigslist", 0)
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})

    assert revise.tell_settled(_deps(store, bus)) == 0
    assert sent == [] and store.listing_revisions_owing_buyers()


def test_a_stale_row_closing_does_not_stop_the_next_from_telling(
    store, bus, make_ctx, sent
) -> None:
    item = _item_on(store, "fb", "craigslist")
    _asked(store, item["id"], "craigslist", 0)
    _drop_to_80(make_ctx, item["id"])
    for _ in range(2):
        claimed = store.claim_listing_revision()
        store.finish_listing_revision(
            claimed["revision_id"], status="done", accepted={"price": "80"}
        )
    with store._db.transaction() as conn:  # noqa: SLF001 — Facebook's landed two days ago
        conn.execute(
            "UPDATE listing_revisions SET finished_ts = finished_ts - 172800 WHERE market = 'fb'"
        )

    assert revise.tell_settled(_lane(store, bus, make_ctx)) == 1
    assert _told(sent) == [("registration", "craigslist:t0")]


def test_a_buyer_updated_before_the_edit_landed_who_wrote_since_is_told(
    store, bus, make_ctx, sent
) -> None:
    item = _item_on(store, "craigslist")
    thread_id = _asked(store, item["id"], "craigslist", 0)
    with store._db.transaction() as conn:  # noqa: SLF001 — an update long before this edit
        conn.execute("UPDATE threads SET last_update_ts = 0.5 WHERE thread_id = ?", (thread_id,))
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})

    revise.tell_settled(_lane(store, bus, make_ctx))
    assert _told(sent) == [("registration", thread_id)]


def test_a_nudge_sent_after_the_edit_landed_does_not_hold_back_its_update(
    store, bus, make_ctx, sent
) -> None:
    item = _item_on(store, "craigslist")
    thread_id = _asked(store, item["id"], "craigslist", 0)
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})
    with store._db.transaction() as conn:  # noqa: SLF001 — a nudge after the edit landed
        conn.execute(
            "UPDATE threads SET last_followup_ts = "
            "(SELECT finished_ts + 1 FROM listing_revisions) WHERE thread_id = ?",
            (thread_id,),
        )

    revise.tell_settled(_lane(store, bus, make_ctx))
    assert _told(sent) == [("registration", thread_id)]


@pytest.mark.parametrize("market", ["fb", "carousell", "craigslist"])
def test_an_edit_made_while_updates_were_off_tells_nobody_once_they_are_back_on(
    store, bus, make_ctx, sent, market
) -> None:
    item = _item_on(store, market)
    _asked(store, item["id"], market, 0)
    seed_setting(store, "past_enquirer_updates", False)
    _drop_to_80(make_ctx, item["id"])
    seed_setting(store, "past_enquirer_updates", True)  # back on before the edit lands
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})

    revise.tell_settled(_lane(store, bus, make_ctx))
    assert sent == []
    assert store.listing_revisions_owing_buyers() == []


@pytest.mark.parametrize("claimed", [False, True], ids=["pending", "running"])
def test_a_drop_made_while_updates_were_off_is_not_revived_by_a_later_edit(
    store, bus, make_ctx, sent, claimed
) -> None:
    """On: 120 to 100. Off: 100 to 80. On again: a title edit. The owed edit must not announce
    120 to 80, which would carry the drop made while updates were off."""
    item = _item_on(store, "craigslist")
    _asked(store, item["id"], "craigslist", 0)
    ctx = make_ctx(TIER_PASS_CHANNEL, pass_id="p1")

    def edit(fields):
        dispatch("update_live_listing", {"item_id": item["id"], "fields": fields}, ctx)

    edit({"list_price": 100})
    if claimed:
        store.claim_listing_revision()  # mid-drive, so the next edit does not supersede it
    seed_setting(store, "past_enquirer_updates", False)
    edit({"list_price": 80})
    seed_setting(store, "past_enquirer_updates", True)
    edit({"title": "Dyson V8 Absolute"})

    newest = max(store.list_listing_revisions("pending"), key=lambda r: r["created_ts"])
    assert (newest["old_price"], newest["new_price"]) != (120.0, 80.0)
    if claimed:
        (running,) = store.list_listing_revisions("running")
        store.finish_listing_revision(running["revision_id"], status="done", accepted={})
    _settle(store, newest["revision_id"], "done", {"price": "80"})
    revise.tell_settled(_lane(store, bus, make_ctx))
    assert sent == []


def test_a_price_edited_between_two_sends_stops_the_rest_of_the_round(
    store, bus, make_ctx, monkeypatch
) -> None:
    """The lane holds the edit lock per send, so a seller's edit can land between two buyers; the
    next buyer must not then be sent the price it replaced."""
    seed_setting(store, "connected_markets", ["carousell", "fb", "craigslist"])
    item = _item_on(store, "craigslist")
    for n in (0, 1):
        _asked(store, item["id"], "craigslist", n)
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})
    sent: list = []

    def send_then_edit(self, thread, text, kind, intent_id):
        sent.append(thread["thread_id"])
        store.revise_item(item["id"], {"list_price": 70.0})  # the seller edits again
        return {}

    monkeypatch.setattr(RegistrationReplySink, "send", send_then_edit)
    rounds: list = []
    send_updates = revise_tool._send_updates
    monkeypatch.setattr(
        revise_tool,
        "_send_updates",
        lambda *a, **kw: rounds.append(send_updates(*a, **kw)) or rounds[-1],
    )
    deps = _lane(store, bus, make_ctx)
    revise.tell_settled(deps)
    assert sent == ["craigslist:t0"]
    # The buyer left untold is still listed, with why.
    assert [(s.thread_id, s.reason) for s in rounds[0].not_told] == [
        ("craigslist:t1", revise_tool._PRICE_MOVED)
    ]


def test_a_price_edited_again_before_the_lane_sends_is_not_announced(
    store, bus, make_ctx, sent, monkeypatch
) -> None:
    item = _item_on(store, "craigslist")
    _asked(store, item["id"], "craigslist", 0)
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})
    read = revise.settled_change

    def then_edited_again(store_, revision):
        change = read(store_, revision)
        store.revise_item(item["id"], {"list_price": 70.0})  # lands before the sender's lock
        return change

    monkeypatch.setattr(revise, "settled_change", then_edited_again)
    revise.tell_settled(_lane(store, bus, make_ctx))
    assert sent == []


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("market", ["fb", "carousell", "craigslist"])
def test_with_updates_off_a_settled_browser_edit_tells_nobody_even_later(
    store, bus, make_ctx, sent, market, enabled
) -> None:
    item = _item_on(store, market)
    _asked(store, item["id"], market, 0)
    _drop_to_80(make_ctx, item["id"])
    (row,) = store.list_listing_revisions("pending")
    _settle(store, row["revision_id"], "done", {"price": "80"})
    seed_setting(store, "past_enquirer_updates", enabled)
    deps = _lane(store, bus, make_ctx)

    revise.tell_settled(deps)
    assert bool(sent) is enabled
    assert any(text.startswith("📣") for text in _seller_hears(store)) is enabled
    assert bool(store.untold_tells()) is enabled
    assert store.listing_revisions_owing_buyers() == []

    seed_setting(store, "past_enquirer_updates", True)  # turned back on: nothing stale goes out
    revise.tell_settled(deps)
    assert bool(sent) is enabled


def _photo_settled(store, market: str, *, by_pass: bool) -> dict:
    item = _item_on(store, market)
    _asked(store, item["id"], market, 0)
    rev = store.queue_listing_revision(
        item["id"], market, ["photos"], old_price=120.0, new_price=120.0, photo_added=True
    )
    claimed = store.claim_listing_revision()
    if by_pass:
        store.attach_revision_pass(rev, "pass_edit")
    store.finish_listing_revision(claimed["revision_id"], status="done", accepted={"price": "120"})
    return item


def test_a_photo_the_craigslist_driver_checked_tells_its_buyers(store, bus, make_ctx, sent):
    _photo_settled(store, "craigslist", by_pass=False)

    revise.tell_settled(_lane(store, bus, make_ctx))
    assert sent == [
        (
            "registration",
            "craigslist:t0",
            f"I've added new photos of the Dyson V8: {CRAIGSLIST_URL}.",
            "followup",
        )
    ]
    assert "📣 Dyson V8 — let 1 buyer who asked know about the new photos." in _seller_hears(store)


def test_a_photo_only_an_edit_pass_reported_tells_nobody(store, bus, make_ctx, sent) -> None:
    _photo_settled(store, "carousell", by_pass=True)

    revise.tell_settled(_lane(store, bus, make_ctx))
    assert sent == [] and store.listing_revisions_owing_buyers() == []


def test_with_updates_off_every_owed_edit_closes_untold(store, bus, make_ctx, sent) -> None:
    item = _item_on(store, "fb", "craigslist")
    _asked(store, item["id"], "fb", 0)
    _asked(store, item["id"], "craigslist", 1)
    _drop_to_80(make_ctx, item["id"])
    for _ in range(2):
        claimed = store.claim_listing_revision()
        store.finish_listing_revision(
            claimed["revision_id"], status="done", accepted={"price": "80"}
        )
    seed_setting(store, "past_enquirer_updates", False)

    revise.tell_settled(_lane(store, bus, make_ctx))
    assert sent == [] and store.listing_revisions_owing_buyers() == []
