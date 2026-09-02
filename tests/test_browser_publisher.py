"""The publish flow: the line a market cannot move, and the gates it cannot skip.

Before the commit a listing does not exist and anything wrong is free; after it, the only safe move
is to stop. Each market writes its own choreography, so what is held here is everything that is
*not* the market's: that the two gates run after `prepare` whatever `prepare` did, that a failure
before the commit stays retryable and a failure after it never is, and that a publish the landing
page cannot name is either found among the seller's own listings or left for a human.

The market is scripted throughout — Facebook's own choreography is held in
`test_browser_facebook_publish.py`.
"""

from __future__ import annotations

import dataclasses

import pytest

from sellee.browser import markets as market_adapters
from sellee.browser import publisher
from sellee.browser.client import BrowserToolError, BrowserUnavailable
from sellee.browser.markets import publishing

_CREATE = "https://www.facebook.com/marketplace/create/item"
_LISTINGS = "https://www.facebook.com/marketplace/you/selling"

_READBACK_JS = "() => the form as it stands"
_RESULT_JS = "() => the listing that was made"


class ScriptedMarket(publishing.PublishSurface):
    """A market whose choreography is a script: it records what it was asked to do, and raises
    whatever the test handed it."""

    market = "scripted"
    readback_js = _READBACK_JS
    result_js = _RESULT_JS

    def __init__(self, *, prepare=None, refuse=None, verify=None, commit=None):
        self.raises = {"prepare": prepare, "refuse": refuse, "verify": verify, "commit": commit}

    def _did(self, client, what):
        client.actions.append(what)
        if self.raises.get(what) is not None:
            raise self.raises[what]

    def prepare(self, client, item, photos, pause) -> None:
        self._did(client, "prepare")

    def refuse_paid_extras(self, client) -> None:
        self._did(client, "refuse")

    def verify_form(self, client, item) -> None:
        self._did(client, "verify")

    def commit(self, client, pause) -> None:
        self._did(client, "commit")


class StubPage:
    """The page the flow reads: the publish result, and the seller's own listings behind it."""

    def __init__(self, *, listing_id="999", listings=None, fail_on=()):
        self.listing_id = listing_id
        self.listings = listings
        self.fail_on = set(fail_on)
        self.actions: list = []

    def navigate_visible(self, url):
        if "navigate" in self.fail_on:
            raise BrowserToolError("the page went away")
        self.actions.append(f"navigate {url}")

    def evaluate(self, function, **kwargs):
        if function in self.fail_on:
            raise BrowserToolError("the page went away")
        if function == _RESULT_JS:
            self.actions.append("read the result")
            return {"listing_id": self.listing_id, "url": _listing_url(self.listing_id)}
        if function == market_adapters.FACEBOOK.my_listings_entry_js:
            return {"url": "/marketplace/profile/1/"}
        if function == market_adapters.FACEBOOK.my_listings_js:
            return {"listings": list(self.listings or []), "active_count": len(self.listings or [])}
        raise AssertionError(f"the flow read an artifact nobody scripted: {function!r}")


def _listing_url(listing_id):
    return f"https://www.facebook.com/marketplace/item/{listing_id}/" if listing_id else ""


_ITEM = {
    "id": "item_1",
    "title": "White Study Desk",
    "list_price": 65.0,
    "currency": "SGD",
    "description": "Light scuffs on the desktop.",
    "condition": "Used - Good",
}


def _row(listing_id, title):
    return {
        "listing_id": listing_id,
        "title": title,
        "url": f"https://www.facebook.com/marketplace/item/{listing_id}/",
        "price": 15.0,
        "price_text": "SGD15",
    }


def _publish(client, surface, **kwargs):
    """Publish through the flow, with the scripted market on an otherwise real adapter — the
    confirmation fallback reads the listings artifacts, which are the adapter's own."""
    adapter = dataclasses.replace(market_adapters.FACEBOOK, publish=surface)
    return publisher.publish(
        client, adapter, _ITEM, create_url=_CREATE, sleep=lambda _s: None, **kwargs
    )


# --- the order the flow imposes -------------------------------------------------------------------


def test_the_gates_run_between_the_market_and_the_commit() -> None:
    """The form is brought to the front, the market fills it, and only then — always — the two
    gates."""
    client, surface = StubPage(), ScriptedMarket()

    outcome = _publish(client, surface)

    assert client.actions == [
        f"navigate {_CREATE}",
        "prepare",
        "refuse",
        "verify",
        "commit",
        "read the result",
    ]
    assert outcome.verified
    assert outcome.listing_id == "999"


def test_a_market_that_fills_nothing_still_meets_both_gates() -> None:
    """The gates are rules about us, not steps in anyone's form: a `prepare` that skips its own
    checks — or does nothing at all — cannot skip these."""

    class FillsNothing(ScriptedMarket):
        def prepare(self, client, item, photos, pause) -> None:
            client.actions.append("prepare (did nothing)")

    client = StubPage()

    _publish(client, FillsNothing())

    assert client.actions[1:4] == ["prepare (did nothing)", "refuse", "verify"]


def test_a_gate_that_refuses_stops_the_publish_dead() -> None:
    client = StubPage()
    surface = ScriptedMarket(verify=publishing.PublishNotAttempted("the title came back wrong"))

    with pytest.raises(publisher.PublishNotAttempted):
        _publish(client, surface)

    assert "commit" not in client.actions


def test_a_form_that_would_spend_money_is_refused_before_it_is_read_back() -> None:
    """Money is the one gate whose failure cannot be undone by a human afterwards."""
    client = StubPage()
    surface = ScriptedMarket(refuse=publishing.PublishNotAttempted("the boost is still on"))

    with pytest.raises(publisher.PublishNotAttempted):
        _publish(client, surface)

    assert client.actions == [f"navigate {_CREATE}", "prepare", "refuse"]


# --- before the commit, nothing exists ------------------------------------------------------------


def test_a_market_that_refuses_while_filling_leaves_the_work_retryable() -> None:
    client = StubPage()
    surface = ScriptedMarket(
        prepare=publishing.PublishNotAttempted("the chooser would not open", retryable=True)
    )

    with pytest.raises(publisher.PublishNotAttempted) as caught:
        _publish(client, surface)

    assert caught.value.retryable
    assert "commit" not in client.actions


def test_a_bare_browser_failure_while_filling_is_a_refusal_not_a_maybe() -> None:
    """A market writes its own `prepare`; one that lets a browser error out must not read as "the
    publish may have gone through", or the item is retired over a click that never landed."""
    client = StubPage()
    surface = ScriptedMarket(prepare=BrowserToolError("the field went away"))

    with pytest.raises(publisher.PublishNotAttempted) as caught:
        _publish(client, surface)

    assert caught.value.retryable
    assert "commit" not in client.actions


def test_a_create_page_that_will_not_open_is_a_refusal_not_a_maybe() -> None:
    """The form was never reached, let alone submitted; read as "may have gone through", a flaky
    navigation would retire the pair over a page that never loaded."""
    client = StubPage(fail_on=["navigate"])
    surface = ScriptedMarket()

    with pytest.raises(publisher.PublishNotAttempted) as caught:
        _publish(client, surface)

    assert caught.value.retryable
    assert "prepare" not in client.actions


def test_a_market_that_says_it_crossed_its_own_line_early_is_believed() -> None:
    """Some forms commit before the button we think of as publish; a market saying so from
    `prepare` is passed through, not downgraded to a retryable refusal."""
    client = StubPage()
    surface = ScriptedMarket(prepare=publishing.PublishUnverified("the draft went live on typing"))

    with pytest.raises(publisher.PublishUnverified):
        _publish(client, surface)


def test_a_browser_that_is_gone_is_not_the_items_fault() -> None:
    """The daemon reports a dead browser once and skips its lanes; turning it into a refusal would
    spend this item's attempts on it."""
    client = StubPage()
    surface = ScriptedMarket(prepare=BrowserUnavailable("no browser server"))

    with pytest.raises(BrowserUnavailable):
        _publish(client, surface)


def test_a_market_with_no_publish_surface_is_refused_outright() -> None:
    stripped = dataclasses.replace(market_adapters.FACEBOOK, publish=None)

    with pytest.raises(publisher.PublishNotAttempted):
        publisher.publish(StubPage(), stripped, _ITEM, create_url=_CREATE, sleep=lambda _s: None)


# --- past the point of no return ------------------------------------------------------------------


def test_a_failure_during_the_commit_is_unverified_and_never_retried() -> None:
    client = StubPage()
    surface = ScriptedMarket(commit=BrowserToolError("the button went away"))

    with pytest.raises(publisher.PublishUnverified):
        _publish(client, surface)


def test_a_market_cannot_call_its_own_commit_failure_retryable() -> None:
    """From the commit on, a listing may exist whatever the market believes; the bracket is not
    the market's to open."""
    client = StubPage()
    surface = ScriptedMarket(
        commit=publishing.PublishNotAttempted("nothing happened, honest", retryable=True)
    )

    with pytest.raises(publisher.PublishUnverified):
        _publish(client, surface)


def test_a_result_that_cannot_be_read_is_unverified() -> None:
    """It runs on a page that just navigated — the likeliest moment for a browser call to fail —
    and a failure there means a listing that probably exists and cannot be named."""
    client = StubPage(fail_on=[_RESULT_JS])

    with pytest.raises(publisher.PublishUnverified):
        _publish(client, ScriptedMarket())


def test_a_publish_whose_listing_cannot_be_named_is_reported_unverified() -> None:
    """Not an error, but not retried into a duplicate either."""
    client = StubPage(listing_id=None)

    outcome = _publish(client, ScriptedMarket())

    assert outcome.verified is False
    assert outcome.listing_id is None
    assert "could not be identified" in outcome.reason


# --- confirming a publish the landing page does not name ------------------------------------------


def test_a_publish_is_confirmed_from_the_sellers_own_listings() -> None:
    """Facebook's selling page carries no listing ids, so the listing is found among the seller's
    own — the listings capability, which is why the flow keeps the whole adapter in hand."""
    client = StubPage(
        listing_id=None, listings=[_row("777", "White Study Desk"), _row("888", "Something")]
    )

    outcome = _publish(client, ScriptedMarket(), listings_url=_LISTINGS)

    assert outcome.verified
    assert outcome.listing_id == "777"


def test_a_market_whose_landing_page_names_nothing_needs_no_result_artifact() -> None:
    """Reading the result is optional; being able to name the listing afterwards is not."""

    class NamesNothing(ScriptedMarket):
        result_js = ""

    client = StubPage(listings=[_row("777", "White Study Desk")])

    outcome = _publish(client, NamesNothing(), listings_url=_LISTINGS)

    assert outcome.listing_id == "777"
    assert "read the result" not in client.actions


def test_two_listings_sharing_the_title_leave_the_publish_unverified() -> None:
    """Claiming either id would record a URL pointing at the wrong listing."""
    client = StubPage(
        listing_id=None,
        listings=[_row("777", "White Study Desk"), _row("888", "White Study Desk")],
    )

    outcome = _publish(client, ScriptedMarket(), listings_url=_LISTINGS)

    assert outcome.verified is False
    assert outcome.listing_id is None


def test_confirmation_is_skipped_when_there_is_nowhere_to_look() -> None:
    client = StubPage(listing_id=None, listings=[_row("777", "White Study Desk")])

    outcome = _publish(client, ScriptedMarket())

    assert outcome.verified is False


def test_listings_that_cannot_be_read_leave_the_publish_unverified_not_failed() -> None:
    """The listing exists either way; the alternative to a title match is a human going to look."""
    client = StubPage(listing_id=None, fail_on=[market_adapters.FACEBOOK.my_listings_js])

    outcome = _publish(client, ScriptedMarket(), listings_url=_LISTINGS)

    assert outcome.verified is False


# --- a surface that could skip a gate is not buildable --------------------------------------------


def test_a_surface_with_no_read_back_artifact_cannot_be_defined() -> None:
    """Nothing is pressed until the form has been read back, so a market with no read-back has no
    publish surface — and finding that out at import is the point."""
    with pytest.raises(TypeError) as caught:

        class NoReadback(publishing.PublishSurface):
            market = "nowhere"

            def prepare(self, client, item, photos, pause) -> None: ...

            def commit(self, client, pause) -> None: ...

            def verify_form(self, client, item) -> None: ...

            def refuse_paid_extras(self, client) -> None: ...

    assert "readback_js" in str(caught.value)


def test_a_surface_that_leaves_a_gate_unwritten_cannot_be_defined() -> None:
    """A market with nothing to refuse still writes the method, so the weakening is visible in its
    own file."""
    with pytest.raises(TypeError) as caught:

        class NoGate(publishing.PublishSurface):
            market = "nowhere"
            readback_js = "() => ({})"

            def prepare(self, client, item, photos, pause) -> None: ...

            def commit(self, client, pause) -> None: ...

            def verify_form(self, client, item) -> None: ...

    assert "refuse_paid_extras" in str(caught.value)


# --- staging the item's photographs ---------------------------------------------------------


def test_photographs_are_staged_from_the_shape_an_item_stores_them_in(
    tmp_path, monkeypatch
) -> None:
    """An item stores each photograph as a mapping, not a path; staging must take the path from
    inside it."""
    from sellee import paths

    monkeypatch.setattr(paths, "publish_staging_dir", lambda: tmp_path / "staging")
    real = tmp_path / "01.jpg"
    real.write_bytes(b"\xff\xd8\xff" + b"0" * 32)

    staged = publisher.stage_photos("item_1", [{"path": str(real), "uploaded_url": "x" * 900}])

    assert len(staged) == 1
    assert open(staged[0], "rb").read() == real.read_bytes()


def test_a_bare_path_still_stages(tmp_path, monkeypatch) -> None:
    from sellee import paths

    monkeypatch.setattr(paths, "publish_staging_dir", lambda: tmp_path / "staging")
    real = tmp_path / "01.jpg"
    real.write_bytes(b"\xff\xd8\xff")

    assert len(publisher.stage_photos("item_1", [str(real)])) == 1


def test_a_photograph_with_no_path_is_skipped_rather_than_crashing(tmp_path, monkeypatch) -> None:
    from sellee import paths

    monkeypatch.setattr(paths, "publish_staging_dir", lambda: tmp_path / "staging")

    assert publisher.stage_photos("item_1", [{"uploaded_url": "x"}, {}, ""]) == []


def test_the_settles_between_form_steps_are_jittered(monkeypatch) -> None:
    """The point is variance, not slowness: the average pause is unchanged."""
    import statistics

    slept: list = []
    monkeypatch.setattr("time.sleep", slept.append)

    for _ in range(400):
        publisher._sleep(publisher.STEP_SETTLE_SEC)

    assert len(set(slept)) > 300, "the pause is effectively constant"
    assert min(slept) >= publisher.STEP_SETTLE_SEC * (1 - publisher._JITTER)
    assert max(slept) <= publisher.STEP_SETTLE_SEC * (1 + publisher._JITTER)
    # Jitter, not delay: the mean is where it always was.
    assert statistics.mean(slept) == pytest.approx(publisher.STEP_SETTLE_SEC, rel=0.08)
