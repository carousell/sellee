"""Taking over the listings a seller already had: the look, the ask, the adoption, the relist.

The shape under test is what makes the feature safe rather than merely working:

  * a market is looked at once, and a look that could not be served is not a look;
  * an empty read and a failed read are different answers, and only one of them stops us asking;
  * a yes is checked against the marketplace *again* before anything is adopted, because the thing
    it approves can sell in the meantime;
  * every boundary is crash-safe — the suite restarts the lane at each one and demands it converge
    without a duplicate item, a duplicate listing, or a lost publish;
  * one bad listing never blocks the ones behind it.
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest
from tests.conftest import seed_setting

from sellee import settings
from sellee.browser import adopt, survey
from sellee.browser.client import BrowserToolError, BrowserUnavailable
from sellee.browser.markets import carousell as carousell_market
from sellee.channel import fastpaths
from sellee.config import Config
from sellee.engines import pacing
from sellee.store.survey import RAIL_DONE, RAIL_FAILED, RAIL_OWED

_MARKET = "carousell"
_LISTINGS_URL = "https://www.carousell.sg/manage-listings/"


def _listing(listing_id="111", title="Teak lamp", price=80.0, price_text="S$80"):
    return {
        "listing_id": listing_id,
        "url": f"https://www.carousell.sg/p/teak-lamp-{listing_id}/",
        "title": title,
        "price": price,
        "price_text": price_text,
    }


def _detail(**overrides):
    detail = {
        "active": True,
        "availability": "https://schema.org/InStock",
        "title": "Teak lamp",
        "description": "A lamp.",
        "price": 80.0,
        "currency": "SGD",
        "condition": "Lightly used",
        "photo_urls": ["https://media.karousell.com/media/photos/products/a.jpg"],
    }
    detail.update(overrides)
    return detail


@pytest.fixture(autouse=True)
def _one_market(carousell_only):
    """Carousell alone: these script one market's artifacts, and a lane tick drives every connected
    market, so leaving Facebook on would read a marketplace no stub here was taught."""


class StubClient:
    """A browser answering the survey artifacts from a script, recording where it was sent.

    Dispatches on the artifact it is handed, so a lane that evaluates something this does not know
    fails as a clear assertion rather than as a confusing None.
    """

    def __init__(self, *, login="logged_in", listings=None, detail=None, fail=None):
        self.login = login
        self.listings = listings
        self.detail = detail
        self.fail = fail
        self.navigations: list = []

    class _Exclusive:
        def __init__(self, client):
            self.client = client

        def __enter__(self):
            return self.client

        def __exit__(self, *exc):
            return False

    def exclusive(self):
        return self._Exclusive(self)

    def navigate(self, url):
        if self.fail == "navigate":
            raise BrowserToolError("navigation refused")
        self.navigations.append(url)

    def evaluate(self, function, **kwargs):
        if function == carousell_market.LOGIN_JS:
            return {"state": self.login}
        if function == carousell_market.MY_LISTINGS_JS:
            if self.fail == "listings":
                raise BrowserToolError("evaluate failed")
            return self.listings
        if function == carousell_market.LISTING_DETAIL_JS:
            if self.fail == "detail":
                raise BrowserToolError("evaluate failed")
            return self.detail() if callable(self.detail) else self.detail
        raise AssertionError(f"the lane evaluated an artifact this stub does not know: {function}")


def _deps(store, bus, client=None, **overrides):
    def factory():
        if client == "unavailable":
            raise BrowserUnavailable("no node")
        return client

    return survey.SurveyDeps(
        store=store,
        bus=bus,
        config=Config(),
        browser_factory=factory,
        **overrides,
    )


def _ready(store):
    """A seller whose region resolves to a Carousell site, so the market is surveyable."""
    store.set_seller_config_section("basics", {"region": "SG", "currency": "SGD"})


def _events(bus, kind):
    return bus.store.read(kinds=[kind])


# --- the look -----------------------------------------------------------------------------------


def test_a_clean_read_asks_once_and_records_the_listings(store, bus) -> None:
    _ready(store)
    store.request_market_survey(_MARKET)
    client = StubClient(
        listings={
            "listings": [_listing(), _listing("222", "Oak chair")],
            "active_count": 2,
            "dropped": 0,
            "truncated": False,
        }
    )

    survey.discover_phase(_deps(store, bus, client))

    rows = store.list_discovered_listings(_MARKET)
    assert [r["listing_id"] for r in rows] == ["111", "222"]
    assert {r["status"] for r in rows} == {"pending"}
    assert store.get_market_survey(_MARKET)["state"] == "done"
    notices = store.list_queued_notices()
    assert len(notices) == 1
    assert "Teak lamp" in notices[0]["text"] and "Oak chair" in notices[0]["text"]
    # Controls round-trip through JSON, so they come back as lists rather than tuples.
    assert notices[0]["controls"] == [
        ["Yes, manage them", f"{_MARKET}:{fastpaths.CB_SURVEY_YES}"],
        ["No thanks", f"{_MARKET}:{fastpaths.CB_SURVEY_NO}"],
    ]

    # Running again asks nothing further: the survey is done and the rows already exist.
    survey.discover_phase(_deps(store, bus, client))
    assert len(store.list_queued_notices()) == 1


def test_nothing_listed_is_surveyed_silently(store, bus) -> None:
    """An empty read is an answer — it means the seller has nothing listed, so there is nothing to
    ask about and the market is not looked at again."""
    _ready(store)
    store.request_market_survey(_MARKET)
    client = StubClient(listings={"listings": [], "active_count": 0, "dropped": 0})

    survey.discover_phase(_deps(store, bus, client))

    assert store.get_market_survey(_MARKET)["state"] == "done"
    assert store.list_queued_notices() == []


def test_an_unreadable_page_is_never_mistaken_for_an_empty_one(store, bus) -> None:
    _ready(store)
    store.request_market_survey(_MARKET)
    client = StubClient(listings={"error": "no listings table on the page"})

    survey.discover_phase(_deps(store, bus, client))

    assert store.get_market_survey(_MARKET)["state"] == "due"
    assert store.get_market_survey(_MARKET)["attempts"] == 1
    assert store.list_queued_notices() == []  # the read lane owns that conversation


def test_a_page_we_could_not_price_is_not_a_seller_with_nothing_listed(store, bus) -> None:
    """The failure mode this guards is silent and permanent: recording an empty survey closes the
    ask-once guard, so a regional site whose prices we cannot parse would mean a seller who is never
    asked and can never be asked again."""
    _ready(store)
    store.request_market_survey(_MARKET)
    listings = {"listings": [], "active_count": 2, "dropped": 2, "unreadable": 2}

    survey.discover_phase(_deps(store, bus, StubClient(listings=listings)))

    assert store.get_market_survey(_MARKET)["state"] == "due"
    assert store.get_market_survey(_MARKET)["attempts"] == 1
    assert not store.list_queued_notices()


def test_signed_out_costs_an_attempt_and_says_nothing(store, bus) -> None:
    _ready(store)
    store.request_market_survey(_MARKET)
    client = StubClient(login="logged_out")

    survey.discover_phase(_deps(store, bus, client))

    assert store.get_market_survey(_MARKET)["attempts"] == 1
    assert store.list_queued_notices() == []


def test_a_market_that_never_reads_is_given_up_on(store, bus) -> None:
    _ready(store)
    store.request_market_survey(_MARKET)
    client = StubClient(listings={"error": "nope"})
    deps = _deps(store, bus, client)

    for _ in range(survey.SURVEY_MAX_ATTEMPTS):
        survey.discover_phase(deps)

    row = store.get_market_survey(_MARKET)
    assert row["state"] == "abandoned"
    assert row["found"] == 0  # "gave up" must never read as "nothing listed"
    assert _events(bus, "survey.abandoned")


def test_a_browser_that_cannot_be_driven_costs_no_attempt(store, bus) -> None:
    _ready(store)
    store.request_market_survey(_MARKET)

    survey.discover_phase(_deps(store, bus, "unavailable"))

    assert store.get_market_survey(_MARKET)["state"] == "due"
    assert store.get_market_survey(_MARKET)["attempts"] == 0


def test_a_pass_holding_the_tab_defers_the_whole_lane(store, bus) -> None:
    _ready(store)
    store.request_market_survey(_MARKET)
    store.enqueue_pass("publish", {"item_id": "item_x", "market": _MARKET})

    survey.survey_lane(_deps(store, bus, StubClient(listings={"listings": [_listing()]})))

    assert store.get_market_survey(_MARKET)["state"] == "due"
    assert store.get_market_survey(_MARKET)["attempts"] == 0


def test_listings_we_already_hold_are_not_offered_again(store, bus) -> None:
    """Everything the agent published itself is on that page too. Offering it back would make a
    second item for one listing."""
    _ready(store)
    item = store.create_item(title="Teak lamp", list_price=80.0, currency="SGD")
    store.record_listing_url(item["id"], _MARKET, _listing()["url"])
    store.request_market_survey(_MARKET)
    client = StubClient(
        listings={"listings": [_listing(), _listing("222", "Oak chair")], "active_count": 2}
    )

    survey.discover_phase(_deps(store, bus, client))

    assert [r["listing_id"] for r in store.list_discovered_listings(_MARKET)] == ["222"]
    assert "Teak lamp" not in store.list_queued_notices()[0]["text"]


def test_a_market_with_no_adapter_is_not_kept_owed(store, bus) -> None:
    _ready(store)
    store.request_market_survey("mercari")

    survey.discover_phase(_deps(store, bus, StubClient()))

    assert store.get_market_survey("mercari")["state"] == "abandoned"


def test_an_unanswered_ask_expires(store, bus) -> None:
    _ready(store)
    store.request_market_survey(_MARKET)
    survey.discover_phase(_deps(store, bus, StubClient(listings={"listings": [_listing()]})))

    # A real clock, moved on. The pacing engine reads the lane's `now` as a wall-clock date — a
    # synthetic epoch would test an hour that cannot happen.
    later = time.time() + survey.DECISION_TTL_SEC + 60
    survey.survey_lane(_deps(store, bus, StubClient(), now=lambda: later))

    assert store.list_discovered_listings(_MARKET)[0]["status"] == "expired"


# --- the ask's two buttons ----------------------------------------------------------------------


def _found(store, bus, listings=None):
    _ready(store)
    store.request_market_survey(_MARKET)
    survey.discover_phase(
        _deps(store, bus, StubClient(listings={"listings": listings or [_listing()]}))
    )


def _tap(store, bus, token):
    return fastpaths.handle_fast_path(
        store, bus, {"kind": "action", "text": "", "payload": {"choice": token, "ref": _MARKET}}
    )


def test_yes_accepts_every_pending_listing_and_never_touches_chrome(store, bus) -> None:
    _found(store, bus, [_listing(), _listing("222")])

    text, controls = _tap(store, bus, fastpaths.CB_SURVEY_YES)

    assert controls is None
    assert "2 listings" in text
    rows = store.list_discovered_listings(_MARKET)
    assert {r["status"] for r in rows} == {"accepted"}
    assert {r["manage"] for r in rows} == {"relist"}


def test_no_declines_them(store, bus) -> None:
    _found(store, bus)

    text, _ = _tap(store, bus, fastpaths.CB_SURVEY_NO)

    assert "leave your Carousell listings alone" in text
    assert store.list_discovered_listings(_MARKET)[0]["status"] == "declined"


def test_a_stale_yes_asks_again_instead_of_adopting(store, bus) -> None:
    """The listings a months-old button named have moved on, and some have sold. Acting on it is
    exactly the mistake the expiry exists to prevent."""
    _found(store, bus)
    store.expire_stale_decisions(0.0)

    text, _ = _tap(store, bus, fastpaths.CB_SURVEY_YES)

    assert "fresh look" in text
    assert store.list_discovered_listings(_MARKET) == []  # the stale list is gone
    assert store.get_market_survey(_MARKET)["state"] == "due"  # and it will be re-read


# --- adoption -----------------------------------------------------------------------------------


def _accepted(store, bus, listings=None, manage="relist"):
    _found(store, bus, listings)
    store.decide_discovered_listings(_MARKET, decision="manage", manage=manage)


def _fetches(monkeypatch, stored=()):
    """Stand in for the network fetch, handing back paths already in the media store."""
    monkeypatch.setattr(adopt.photo_fetch, "fetch_listing_photos", lambda *a, **k: list(stored))


def _media_photo(name="01.jpg") -> str:
    """A real jpeg inside the media store, so `validate_photos` accepts it as it would in life."""
    from sellee import paths

    dest = paths.media_dir() / "adopted-carousell-111" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"\xff\xd8\xff" + b"0" * 32)
    return str(dest)


def test_adoption_writes_the_item_with_its_listing_url(store, bus, monkeypatch, xdg_tmp) -> None:
    _accepted(store, bus)
    _fetches(monkeypatch, [_media_photo()])

    adopt.adopt_phase(_deps(store, bus, StubClient(detail=_detail())))

    row = store.list_discovered_listings(_MARKET)[0]
    assert row["status"] == "adopted"
    item = store.get_item(row["item_id"])
    assert item["listing_urls"][_MARKET] == _listing()["url"]
    assert item["status"] == "ready"
    assert item["list_price"] == 80.0 and item["currency"] == "SGD"
    assert item["condition"] == "Lightly used"
    assert row["rail_state"] == RAIL_OWED


def test_a_listing_that_has_sold_since_the_yes_is_never_adopted(
    store, bus, monkeypatch, xdg_tmp
) -> None:
    _accepted(store, bus)
    _fetches(monkeypatch)
    detail = _detail(active=False, availability="https://schema.org/SoldOut")

    adopt.adopt_phase(_deps(store, bus, StubClient(detail=detail)))

    row = store.list_discovered_listings(_MARKET)[0]
    assert row["status"] == "failed"
    assert "no longer for sale" in row["last_error"]
    assert store.list_items() == []


def test_a_listing_with_no_price_is_dropped_rather_than_made_unpublishable(
    store, bus, monkeypatch, xdg_tmp
) -> None:
    _accepted(store, bus)
    _fetches(monkeypatch)

    adopt.adopt_phase(_deps(store, bus, StubClient(detail=_detail(price=None, currency=None))))

    assert store.list_discovered_listings(_MARKET)[0]["status"] == "failed"
    assert store.list_items() == []


def test_no_photos_still_adopts_for_the_inbox_and_says_so(store, bus, monkeypatch, xdg_tmp) -> None:
    _accepted(store, bus)
    _fetches(monkeypatch, [])

    adopt.adopt_phase(_deps(store, bus, StubClient(detail=_detail())))

    row = store.list_discovered_listings(_MARKET)[0]
    assert row["status"] == "adopted"
    assert row["rail_state"] is None  # nothing to publish without pictures
    assert any("couldn't bring its photos across" in n["text"] for n in store.list_queued_notices())


def test_one_bad_listing_does_not_block_the_next(store, bus, monkeypatch, xdg_tmp) -> None:
    _accepted(store, bus, [_listing("111", "Bad"), _listing("222", "Good")])
    _fetches(monkeypatch)
    # The first listing's page never reads; the second's does.
    details = {"111": None, "222": _detail(title="Good")}
    client = StubClient()
    client.detail = lambda: details[client.navigations[-1].rsplit("-", 1)[1].rstrip("/")]
    deps = _deps(store, bus, client)

    # Two ticks is the whole test: the bad listing is tried first (it is older), and the second tick
    # must move on to the good one rather than retrying the bad one. Running until everything
    # settles would pass under plain age ordering too, and prove nothing.
    adopt.adopt_phase(deps)
    adopt.adopt_phase(deps)

    rows = {r["listing_id"]: r for r in store.list_discovered_listings(_MARKET)}
    assert rows["222"]["status"] == "adopted", "the good listing must not sit behind the bad one"
    assert rows["111"]["attempts"] == 1, "the bad listing must not have been retried ahead of it"

    # And it is still retired in the end rather than left accepted forever.
    for _ in range(adopt.ADOPT_MAX_ATTEMPTS + 1):
        adopt.adopt_phase(deps)
    assert store.list_discovered_listings(_MARKET)[0]["status"] == "failed"


def test_adoption_is_idempotent_after_a_crash(store, bus, monkeypatch, xdg_tmp) -> None:
    """A crash between writing the item and advancing the row leaves the row accepted and the item
    already recording the listing. The retry must link, never create a second item."""
    _accepted(store, bus)
    _fetches(monkeypatch)
    adopt.adopt_phase(_deps(store, bus, StubClient(detail=_detail())))
    item_id = store.list_discovered_listings(_MARKET)[0]["item_id"]

    # Rewind the row to how a crash would have left it, and run again.
    with store._db.transaction() as conn:  # noqa: SLF001 — arranging a crash shape
        conn.execute(
            "UPDATE discovered_listings SET status = 'accepted', item_id = NULL, rail_state = NULL"
        )
    adopt.adopt_phase(_deps(store, bus, StubClient(detail=_detail())))

    assert len(store.list_items()) == 1, "a retry must not make a second item for one listing"
    row = store.list_discovered_listings(_MARKET)[0]
    assert row["status"] == "adopted" and row["item_id"] == item_id


def test_the_batch_summary_accounts_for_what_was_skipped(store, bus, monkeypatch, xdg_tmp) -> None:
    _accepted(store, bus, [_listing("111", "Sold one"), _listing("222", "Live one")])
    _fetches(monkeypatch)
    details = {
        "111": _detail(active=False, availability="https://schema.org/SoldOut"),
        "222": _detail(title="Live one"),
    }
    client = StubClient()
    client.detail = lambda: details[client.navigations[-1].rsplit("-", 1)[1].rstrip("/")]
    deps = _deps(store, bus, client)

    adopt.adopt_phase(deps)
    adopt.adopt_phase(deps)

    summaries = [
        n["text"] for n in store.list_queued_notices() if n["text"].startswith("Done with")
    ]
    assert len(summaries) == 1
    assert "1 taken over" in summaries[0] and "1 skipped" in summaries[0]


# --- the carousell.ai publish ---------------------------------------------------------------------


def _at_local_hour(hour: int) -> float:
    """A timestamp whose *local* hour is `hour`. The pacing engine reads the clock in local time, so
    a test about quiet hours has to name an hour rather than an epoch."""
    return datetime(2026, 3, 1, hour, 30).timestamp()


def _adopted(store, bus, monkeypatch):
    _accepted(store, bus)
    _fetches(monkeypatch, [_media_photo()])
    adopt.adopt_phase(_deps(store, bus, StubClient(detail=_detail())))
    # Quiet hours default on (23:00–08:00) and the publish phase holds inside them — without this
    # every test below would pass or fail on the hour the suite happened to run. Tests about the
    # window turn it back on and pass a fixed clock.
    seed_setting(store, "quiet_hours", [0, 0])
    return store.list_discovered_listings(_MARKET)[0]


def test_an_owed_publish_waits_for_the_slot_and_is_never_lost(
    store, bus, monkeypatch, xdg_tmp
) -> None:
    """The hole this column exists to close: another publish holds the one slot, so this one is
    skipped — and nothing else in the system could ever pick it back up."""
    row = _adopted(store, bus, monkeypatch)
    assert row["rail_state"] == RAIL_OWED
    other = store.enqueue_pass("publish", {"item_id": "item_other", "market": "carousell"})

    adopt.rail_publish_phase(_deps(store, bus, StubClient()))
    assert store.list_discovered_listings(_MARKET)[0]["rail_state"] == RAIL_OWED

    store.finish_pass(other, status="done", rc=0, cls="ok", summary="ok")
    adopt.rail_publish_phase(_deps(store, bus, StubClient()))

    settled = store.list_discovered_listings(_MARKET)[0]
    assert settled["rail_state"] == "queued"
    queued = store.get_pass(settled["rail_pass_id"])
    assert queued["payload"] == {
        "item_id": settled["item_id"],
        "market": "carousell-ai",
        "origin": adopt.ORIGIN,
    }


def test_a_landed_publish_reports_the_link(store, bus, monkeypatch, xdg_tmp) -> None:
    row = _adopted(store, bus, monkeypatch)
    deps = _deps(store, bus, StubClient())
    adopt.rail_publish_phase(deps)
    row = store.list_discovered_listings(_MARKET)[0]
    store.finish_pass(row["rail_pass_id"], status="done", rc=0, cls="ok", summary="ok")
    store.record_listing_url(row["item_id"], "carousell-ai", "https://carousell.ai/listing/abc")

    adopt.rail_publish_phase(deps)

    assert store.list_discovered_listings(_MARKET)[0]["rail_state"] == RAIL_DONE
    assert any("https://carousell.ai/listing/abc" in n["text"] for n in store.list_queued_notices())


def test_a_publish_that_recorded_no_url_is_retried_then_reported_honestly(
    store, bus, monkeypatch, xdg_tmp
) -> None:
    """The verdict comes from the recorded URL, never the exit code — and the failure copy must not
    be the fan-out's, which asserts a carousell.ai listing already exists."""
    _adopted(store, bus, monkeypatch)
    deps = _deps(store, bus, StubClient())

    for _ in range(adopt.RAIL_MAX_ATTEMPTS):
        adopt.rail_publish_phase(deps)
        row = store.list_discovered_listings(_MARKET)[0]
        store.finish_pass(row["rail_pass_id"], status="done", rc=0, cls="ok", summary="clean exit")
        adopt.rail_publish_phase(deps)

    row = store.list_discovered_listings(_MARKET)[0]
    assert row["rail_state"] == RAIL_FAILED
    failure = [n["text"] for n in store.list_queued_notices() if "couldn't get" in n["text"]]
    assert len(failure) == 1
    assert "only on Carousell" in failure[0]
    assert "including its carousell.ai listing" not in failure[0]

    # And the door back the notice promises actually works.
    assert store.decide_discovered_listings(_MARKET, decision="retry") == 1
    assert store.list_discovered_listings(_MARKET)[0]["rail_state"] == RAIL_OWED


def test_a_vanished_publish_pass_is_re_owed_not_stranded(store, bus, monkeypatch, xdg_tmp) -> None:
    """The crash shape for the last boundary: the row says a publish is in flight and the pass is
    gone (swept, or the daemon died mid-enqueue). Recovery has to be observable, so this asserts a
    *different* pass id — "still queued" is what a lane doing nothing would leave behind."""
    _adopted(store, bus, monkeypatch)
    store.set_rail_publish_queued(_MARKET, "111", "pass_gone")

    adopt.rail_publish_phase(_deps(store, bus, StubClient()))

    row = store.list_discovered_listings(_MARKET)[0]
    assert row["rail_state"] == "queued"
    assert row["rail_pass_id"] not in (None, "pass_gone"), "the lost publish was never re-queued"
    assert store.get_pass(row["rail_pass_id"])["status"] == "queued"


def test_quiet_hours_hold_a_publish_instead_of_failing_it(store, bus, monkeypatch, xdg_tmp) -> None:
    """The publish tool refuses inside quiet hours, and a refused pass records no URL — which reads
    as a failed publish. Unheld, an adoption at 11pm burns all three attempts before morning and
    reports every listing as failed with nothing wrong with any of them."""
    _adopted(store, bus, monkeypatch)
    seed_setting(store, "quiet_hours", [2300, 800])

    adopt.rail_publish_phase(_deps(store, bus, StubClient(), now=lambda: _at_local_hour(3)))

    held = store.list_discovered_listings(_MARKET)[0]
    assert held["rail_state"] == RAIL_OWED
    assert held["rail_attempts"] == 0, "a held publish spent an attempt on the clock"
    assert not [p for p in store.publish_pass_index() if p["origin"] == adopt.ORIGIN]
    assert not [n for n in store.list_queued_notices() if "couldn't get" in n["text"]]

    # And the morning tick takes it, with every attempt still intact.
    adopt.rail_publish_phase(_deps(store, bus, StubClient(), now=lambda: _at_local_hour(10)))
    assert store.list_discovered_listings(_MARKET)[0]["rail_state"] == "queued"


def test_the_hourly_cap_holds_a_publish_instead_of_failing_it(
    store, bus, monkeypatch, xdg_tmp
) -> None:
    """The same shape as quiet hours, and the one that bites a seller with more listings than the
    cap: a publish past it is refused, and spending an attempt on that reports listings as failed
    for no reason other than their place in the queue."""
    _adopted(store, bus, monkeypatch)
    cfg = pacing.resolve(Config(), settings.quiet_window_minutes(store))
    for _ in range(cfg.cap):
        store.reserve_action(marketplace="carousell-ai", kind="publish", cfg=cfg)

    adopt.rail_publish_phase(_deps(store, bus, StubClient()))

    held = store.list_discovered_listings(_MARKET)[0]
    assert held["rail_state"] == RAIL_OWED
    assert held["rail_attempts"] == 0
    assert not [p for p in store.publish_pass_index() if p["origin"] == adopt.ORIGIN]


def test_an_item_already_on_the_rail_owes_nothing(store, bus, monkeypatch, xdg_tmp) -> None:
    row = _adopted(store, bus, monkeypatch)
    store.record_listing_url(row["item_id"], "carousell-ai", "https://carousell.ai/listing/zzz")

    adopt.rail_publish_phase(_deps(store, bus, StubClient()))

    assert store.list_discovered_listings(_MARKET)[0]["rail_state"] == RAIL_DONE
    assert not [p for p in store.publish_pass_index() if p["origin"] == adopt.ORIGIN]


# --- crash safety at the discovery boundary ------------------------------------------------------


def test_recording_and_asking_are_one_transaction(store, bus, monkeypatch) -> None:
    """If the ask could fail after the survey closed, the seller would never be asked and no later
    tick would repair it — the one-ask guard is already spent."""
    _ready(store)
    store.request_market_survey(_MARKET)
    import sellee.store.survey as survey_store

    def explode(*args, **kwargs):
        raise RuntimeError("crash between recording and asking")

    monkeypatch.setattr(survey_store, "_insert_notice", explode)
    with pytest.raises(RuntimeError):
        survey.discover_phase(_deps(store, bus, StubClient(listings={"listings": [_listing()]})))

    assert store.list_discovered_listings(_MARKET) == []
    assert store.get_market_survey(_MARKET)["state"] == "due"


# --- the two triggers -----------------------------------------------------------------------------


def test_signing_in_lines_up_a_look_at_what_they_already_have(store, bus) -> None:
    """The seller-initiated moment: they tapped Sign in, and now they are in."""
    from sellee.browser import connect

    _ready(store)
    store.request_market_connect(_MARKET, "probe")
    deps = connect.ConnectDeps(
        store=store, bus=bus, config=Config(), browser_factory=lambda: StubClient()
    )

    connect.connect_lane(deps)

    assert store.get_market_survey(_MARKET)["state"] == "due"
    assert _events(bus, "survey.requested")


def test_a_market_connected_before_any_of_this_is_still_asked_about(store, bus) -> None:
    """The backfill half: the read lane's probe has already told us this market is signed in, so
    reaching a seller who connected it long ago costs nothing extra."""
    from tests.test_browser_inbox import StubClient as InboxStub
    from tests.test_browser_inbox import _deps as _inbox_deps

    _ready(store)
    client = InboxStub(conversations=[])

    inbox_module = __import__("sellee.browser.inbox", fromlist=["inbox_lane"])
    inbox_module.inbox_lane(_inbox_deps(store, bus, client))

    assert store.get_market_survey(_MARKET)["state"] == "due"


def test_a_signed_out_market_is_not_lined_up(store, bus) -> None:
    from tests.test_browser_inbox import StubClient as InboxStub
    from tests.test_browser_inbox import _deps as _inbox_deps

    _ready(store)
    inbox_module = __import__("sellee.browser.inbox", fromlist=["inbox_lane"])
    inbox_module.inbox_lane(_inbox_deps(store, bus, InboxStub(login="logged_out")))

    assert store.get_market_survey(_MARKET) is None


def test_the_ask_is_reachable_from_catchup_after_it_scrolls_away(store, bus) -> None:
    _found(store, bus, [_listing(), _listing("222")])

    assert "2 waiting on whether I should manage them" in fastpaths.render_catchup(store)


# --- the free-text answers ------------------------------------------------------------------------


def test_the_tools_carry_the_answers_a_button_cannot(store, bus, make_ctx) -> None:
    """ "Only the bike", "just answer buyers, don't repost them" — a two-button ask cannot hold
    either, and a seller will say both."""
    from sellee.tools.registry import TIER_PASS_CHANNEL, dispatch

    _found(store, bus, [_listing("111", "Bike"), _listing("222", "Camera")])
    ctx = make_ctx(TIER_PASS_CHANNEL)

    listed = dispatch("list_discovered_listings", {"market": _MARKET}, ctx)
    assert {row["listing_id"] for row in listed["listings"]} == {"111", "222"}

    out = dispatch(
        "decide_discovered_listings",
        {"market": _MARKET, "decision": "manage", "manage": "inbox", "listing_ids": ["111"]},
        ctx,
    )

    assert out["decided"] == 1
    rows = {r["listing_id"]: r for r in store.list_discovered_listings(_MARKET)}
    assert rows["111"]["status"] == "accepted" and rows["111"]["manage"] == "inbox"
    assert rows["222"]["status"] == "pending"


def test_deciding_nothing_is_reported_as_nothing(store, bus, make_ctx) -> None:
    """Zero is an answer, not an error — it is what stops a reply claiming something was taken over
    when the listings had already been decided."""
    from sellee.tools.registry import TIER_PASS_CHANNEL, dispatch

    _ready(store)
    ctx = make_ctx(TIER_PASS_CHANNEL)

    out = dispatch("decide_discovered_listings", {"market": _MARKET, "decision": "decline"}, ctx)

    assert out["decided"] == 0


def test_an_inbox_only_adoption_owes_no_rail_publish(store, bus, monkeypatch, xdg_tmp) -> None:
    """The half a button cannot express: answer buyers on it where it is, and do not repost it."""
    _found(store, bus)
    store.decide_discovered_listings(_MARKET, decision="manage", manage="inbox")
    _fetches(monkeypatch, [_media_photo()])

    adopt.adopt_phase(_deps(store, bus, StubClient(detail=_detail())))

    row = store.list_discovered_listings(_MARKET)[0]
    assert row["status"] == "adopted"
    assert row["rail_state"] is None
    assert store.get_item(row["item_id"])["listing_urls"] == {_MARKET: _listing()["url"]}


def test_two_items_claiming_one_listing_is_never_adopted(store, bus, monkeypatch, xdg_tmp) -> None:
    """The read lane refuses to attach a conversation when two items claim one listing, because
    attaching to the wrong one negotiates against the wrong floor. Adoption meets the same
    ambiguity, and creating a third item would be the worst of the three answers."""
    _accepted(store, bus)
    for title in ("Copy A", "Copy B"):
        item = store.create_item(title=title, list_price=80.0, currency="SGD")
        store.record_listing_url(item["id"], _MARKET, _listing()["url"])
    _fetches(monkeypatch, [_media_photo()])

    adopt.adopt_phase(_deps(store, bus, StubClient(detail=_detail())))

    assert len(store.list_items()) == 2, "no third item for a listing two already claim"
    row = store.list_discovered_listings(_MARKET)[0]
    assert row["status"] == "failed"
    assert "already claim this listing" in row["last_error"]


# --- the buttons stay on the message forever, so every tap is a question about when ---------------


def test_tapping_yes_twice_does_not_throw_away_the_first_yes(store, bus) -> None:
    """The buttons are never removed from the message. A seller checking whether their yes landed
    taps it again — which used to delete every listing that yes had accepted and tell them the list
    was out of date."""
    _found(store, bus, [_listing(), _listing("222")])
    first, _ = _tap(store, bus, fastpaths.CB_SURVEY_YES)
    assert "2 listings" in first

    second, _ = _tap(store, bus, fastpaths.CB_SURVEY_YES)

    rows = store.list_discovered_listings(_MARKET)
    assert [r["status"] for r in rows] == ["accepted", "accepted"], "the yes must survive a re-tap"
    assert "out of date" not in second
    assert store.get_market_survey(_MARKET)["state"] == "done", "and it must not re-ask"


def test_no_after_yes_actually_stops_the_work(store, bus) -> None:
    """Changing your mind between the tap and the lane getting there is ordinary. A decline that
    only reached pending rows would report leaving listings alone while taking them over."""
    _found(store, bus, [_listing(), _listing("222")])
    _tap(store, bus, fastpaths.CB_SURVEY_YES)

    text, _ = _tap(store, bus, fastpaths.CB_SURVEY_NO)

    assert "leave your Carousell listings alone" in text
    assert {r["status"] for r in store.list_discovered_listings(_MARKET)} == {"declined"}


def test_no_after_the_work_is_done_says_what_is_true(store, bus, monkeypatch, xdg_tmp) -> None:
    _accepted(store, bus)
    _fetches(monkeypatch, [_media_photo()])
    adopt.adopt_phase(_deps(store, bus, StubClient(detail=_detail())))

    text, _ = _tap(store, bus, fastpaths.CB_SURVEY_NO)

    assert "already taken over" in text
    assert store.list_discovered_listings(_MARKET)[0]["status"] == "adopted"


def test_a_declined_listing_is_not_adopted_by_a_lane_already_reading_it(
    store, bus, monkeypatch, xdg_tmp
) -> None:
    """The adopt lane reads the row, then spends a page read and a photo fetch on it. A decline
    landing in that window has to win — and must not leave an item behind with nothing to publish
    it, since nothing else could ever recover the carousell.ai listing it was owed."""
    _accepted(store, bus)
    _fetches(monkeypatch, [_media_photo()])
    client = StubClient()

    def decline_then_answer():
        store.decide_discovered_listings(_MARKET, decision="decline")
        return _detail()

    client.detail = decline_then_answer

    adopt.adopt_phase(_deps(store, bus, client))

    assert store.list_items() == [], "no item for a listing the seller just declined"
    assert store.list_discovered_listings(_MARKET)[0]["status"] == "declined"
    assert _events(bus, "survey.adopt_dropped")


def test_a_listing_capped_mid_crash_is_still_retired(store, bus, monkeypatch, xdg_tmp) -> None:
    """The attempt bump and the retirement are separate transactions, so a crash between them used
    to leave the row accepted with its attempts spent — invisible to the lane, and blocking the
    batch summary behind it forever."""
    _accepted(store, bus)
    with store._db.transaction() as conn:  # noqa: SLF001 — arranging the crash shape
        conn.execute(
            "UPDATE discovered_listings SET attempts = ?, last_error = 'page would not read'",
            (adopt.ADOPT_MAX_ATTEMPTS,),
        )

    adopt.adopt_phase(_deps(store, bus, StubClient()))

    row = store.list_discovered_listings(_MARKET)[0]
    assert row["status"] == "failed"
    assert "page would not read" in row["last_error"]
    assert any(n["text"].startswith("Done with") for n in store.list_queued_notices())


def test_a_partial_listings_read_never_closes_the_survey(store, bus) -> None:
    """Asking on a half-read list would close the ask-once survey, so the seller would be asked
    about some of their listings and never about the rest."""
    _ready(store)
    store.request_market_survey(_MARKET)
    client = StubClient(
        listings={"listings": [_listing()], "active_count": 40, "dropped": 0, "truncated": True}
    )

    survey.discover_phase(_deps(store, bus, client))

    assert store.get_market_survey(_MARKET)["state"] == "due"
    assert store.list_discovered_listings(_MARKET) == []
    assert store.list_queued_notices() == []


def test_the_ask_never_offers_to_relist_somewhere_it_already_is(store, bus) -> None:
    """A seller whose enabled marketplaces include the one being surveyed must not be told their
    Carousell listings will be put on Carousell."""
    seed_setting(store, "connected_markets", [_MARKET])
    _found(store, bus)

    text = store.list_queued_notices()[0]["text"]

    assert "list them on Carousell.ai too" in text
    assert "and Carousell too" not in text
