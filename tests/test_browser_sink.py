"""The scripted marketplace send: what it does, and what it refuses to do twice.

The two failure shapes carry opposite consequences, so they get the most attention here: a send
that never happened must stay retryable, and one that happened but could not be confirmed must
never be re-driven — a buyer getting the same message twice is what this design exists to avoid.
"""

from __future__ import annotations

import json
import re

import pytest

from selly_agent.browser import markets as market_adapters
from selly_agent.browser import selectors, sink
from selly_agent.browser.client import BrowserToolError
from selly_agent.browser.markets import carousell as carousell_market
from selly_agent.config import Config

_THREAD_URL = "https://www.carousell.sg/inbox/99/"
_FAST = Config(reply_delay_sec=(0, 0), interactive_reply_delay_sec=(0, 0))


class StubClient:
    """A browser whose composer is present or absent per the test, and whose page remembers what was
    typed so a verify read-back can be real rather than asserted."""

    def __init__(self, *, matches=None, sent_bubbles=None, fail_on=None, echo_on_click=True):
        self.matches = {"textarea": 1, 'button[aria-label="Send"], button[type="submit"]': 1}
        if matches is not None:
            self.matches = matches
        self.bubbles = list(sent_bubbles or [])
        self.fail_on = fail_on
        self.echo_on_click = echo_on_click
        self.calls: list = []
        self.typed: str | None = None
        self.url = ""

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
        self.calls.append(("navigate", url))
        if self.fail_on == "navigate":
            raise BrowserToolError("navigation refused")
        self.url = url

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.fail_on == name:
            raise BrowserToolError(f"{name} refused")
        if name == "browser_type":
            self.typed = arguments["text"]
        if name == "browser_click" and self.echo_on_click and self.typed is not None:
            self.bubbles.append({"text": self.typed, "side": "out", "y": 99})
        return "ok"

    def evaluate(self, function, **kwargs):
        if function == carousell_market.TAIL_JS:
            return list(self.bubbles)
        # a locate probe: read the target back out of the probe's own source, so this stub answers
        # the selector the sink actually asked about
        found = re.search(r"querySelectorAll\((\".*?\")\)", function)
        if not found:
            raise AssertionError(f"unexpected evaluate: {function}")
        target = json.loads(found.group(1))
        return {"matches": self.matches.get(target, 0), "url": self.url}


def _sink(store, bus, client):
    return sink.BrowserReplySink(client=client, store=store, bus=bus, region="SG")


@pytest.fixture
def thread(store):
    item = store.create_item(title="Teak lamp", list_price=80.0, currency="SGD")
    store.create_thread(
        thread_id="carousell:99",
        side="sell",
        market="carousell",
        counterpart_handle="bob",
        item_id=item["id"],
    )
    return store.get_thread("carousell:99")


def _reserve(store, thread_id="carousell:99"):
    from selly_agent.engines import pacing

    reserved = store.reserve_reply(
        thread_id=thread_id,
        kind="reply",
        text="yes, still available!",
        in_msg_id="m1",
        cfg=pacing.resolve(_FAST, quiet_hours=(0, 0)),
    )
    return reserved["intent_id"]


def _intent_status(store, intent_id):
    rows = store._db.query(  # noqa: SLF001 — reading the bracket's own row
        "SELECT status FROM send_intents WHERE intent_id = ?", (intent_id,)
    )
    return rows[0]["status"]


def _events(bus, kind):
    return bus.store.read(kinds=[kind])


# --- the send -----------------------------------------------------------------------------------


def test_a_send_types_clicks_and_verifies_on_the_recorded_thread_url(store, bus, thread) -> None:
    client = StubClient()
    intent = _reserve(store)
    _sink(store, bus, client).send(thread, "yes, still available!", "reply", intent)

    names = [call[0] for call in client.calls]
    assert names[0] == "navigate" and client.calls[0][1] == _THREAD_URL
    assert "browser_type" in names and "browser_click" in names
    assert names.index("browser_type") < names.index("browser_click")
    assert [e.payload["outcome"] for e in _events(bus, "browser.send")] == ["sent"]


def test_the_send_leaves_the_intent_for_the_tool_to_commit(store, bus, thread) -> None:
    """The sink's job ends at "it is on the page"; the transcript row and the cursor advance are the
    tool's second transaction."""
    client = StubClient()
    intent = _reserve(store)
    _sink(store, bus, client).send(thread, "yes, still available!", "reply", intent)
    assert _intent_status(store, intent) == "sent_unverified"
    assert store.get_thread("carousell:99")["messages"] == []


def test_the_thread_url_is_never_composed_by_hand(store, bus, thread) -> None:
    client = StubClient()
    _sink(store, bus, client).send(thread, "hi", "reply", _reserve(store))
    navigations = [args for name, args in client.calls if name == "navigate"]
    assert navigations == [_THREAD_URL]


# --- nothing sent: fail closed, stay retryable ---------------------------------------------------


def test_a_composer_miss_fails_before_anything_is_typed(store, bus, thread) -> None:
    """A selector that resolves to nothing must stop the send while it is still safe to retry."""
    client = StubClient(matches={"textarea": 0})
    intent = _reserve(store)
    with pytest.raises(sink.SendNotAttempted, match="message_box"):
        _sink(store, bus, client).send(thread, "hi", "reply", intent)
    assert [name for name, _ in client.calls] == ["navigate"]  # nothing typed, nothing clicked
    assert _intent_status(store, intent) == "pending"  # retry-safe


def test_several_matches_is_a_miss_not_a_pick(store, bus, thread) -> None:
    """Choosing one of several would be a silent guess on the account-sensitive path."""
    client = StubClient(matches={"textarea": 3})
    with pytest.raises(sink.SendNotAttempted):
        _sink(store, bus, client).send(thread, "hi", "reply", _reserve(store))


def test_a_missing_send_button_stops_the_send_after_locating_the_box(store, bus, thread) -> None:
    client = StubClient(matches={"textarea": 1})
    intent = _reserve(store)
    with pytest.raises(sink.SendNotAttempted, match="send_button"):
        _sink(store, bus, client).send(thread, "hi", "reply", intent)
    assert "browser_type" not in [name for name, _ in client.calls]
    assert _intent_status(store, intent) == "pending"


def test_a_browser_failure_while_typing_leaves_the_intent_pending(store, bus, thread) -> None:
    client = StubClient(fail_on="browser_type")
    intent = _reserve(store)
    with pytest.raises(sink.SendNotAttempted):
        _sink(store, bus, client).send(thread, "hi", "reply", intent)
    assert _intent_status(store, intent) == "pending"


def test_an_unknown_market_is_refused_before_any_navigation(store, bus) -> None:
    item = store.create_item(title="Lamp", list_price=10.0, currency="SGD")
    store.create_thread(
        thread_id="ebay:1", side="sell", market="ebay", counterpart_handle="x", item_id=item["id"]
    )
    client = StubClient()
    with pytest.raises(sink.SendNotAttempted, match="adapter"):
        _sink(store, bus, client).send(store.get_thread("ebay:1"), "hi", "reply", "intent_x")
    assert client.calls == []


# --- sent but unconfirmed: verify, never re-drive ------------------------------------------------


def test_a_send_we_cannot_confirm_stays_sent_unverified_and_is_never_resent(store, bus, thread):
    """The one thing worse than an unconfirmed message is the same message twice, so this hands the
    thread to the sweep rather than clicking send again."""
    client = StubClient(echo_on_click=False)  # the click appeared to work but nothing landed
    intent = _reserve(store)
    with pytest.raises(sink.SendUnverified):
        _sink(store, bus, client).send(thread, "yes, still available!", "reply", intent)
    assert _intent_status(store, intent) == "sent_unverified"
    assert [name for name, _ in client.calls].count("browser_click") == 1
    assert [e.payload["outcome"] for e in _events(bus, "browser.send")] == ["unverified"]


def test_the_sweep_escalates_an_unverified_send_without_resending(store, bus, thread) -> None:
    from selly_agent import intent_sweep

    client = StubClient(echo_on_click=False)
    intent = _reserve(store)
    with pytest.raises(sink.SendUnverified):
        _sink(store, bus, client).send(thread, "hi", "reply", intent)
    folded = intent_sweep.run_stale_intent_sweep(bus=bus, store=store, grace_sec=0)
    assert len(folded) == 1 and folded[0]["escalation_new"] is True
    assert _intent_status(store, intent) == "unconfirmed"
    assert store.get_thread("carousell:99")["status"] == "escalated"


def test_no_error_from_the_click_is_not_treated_as_success(store, bus, thread) -> None:
    """A refused validation, a cleared composer, or a swallowed click all look fine from outside;
    only our own words in an outbound bubble count."""
    client = StubClient(echo_on_click=False, sent_bubbles=[{"text": "hi", "side": "in", "y": 1}])
    with pytest.raises(sink.SendUnverified):
        _sink(store, bus, client).send(thread, "hi", "reply", _reserve(store))


def test_an_inbound_bubble_with_our_text_does_not_count_as_verification(store, bus, thread):
    client = StubClient(
        echo_on_click=False, sent_bubbles=[{"text": "same words", "side": "in", "y": 1}]
    )
    with pytest.raises(sink.SendUnverified):
        _sink(store, bus, client).send(thread, "same words", "reply", _reserve(store))


# --- the heal overlay ---------------------------------------------------------------------------


def test_a_healed_selector_is_used_ahead_of_the_shipped_default(store, bus, thread) -> None:
    store.ui_cache_record(
        market="carousell",
        flow="reply",
        step="message_box",
        strategy="css",
        query="div.new-composer",
        page_url_pattern="/inbox/",
        action_kind="type",
    )
    client = StubClient(
        matches={
            "div.new-composer": 1,
            'button[aria-label="Send"], button[type="submit"]': 1,
        }
    )
    _sink(store, bus, client).send(thread, "hi", "reply", _reserve(store))
    typed = [args for name, args in client.calls if name == "browser_type"]
    assert typed[0]["target"] == "div.new-composer"


def test_a_stale_cache_row_is_skipped_for_the_shipped_default(store, bus, thread) -> None:
    store.ui_cache_record(
        market="carousell",
        flow="reply",
        step="message_box",
        strategy="css",
        query="div.old-composer",
        page_url_pattern="/inbox/",
        action_kind="type",
    )
    for _ in range(3):
        store.ui_cache_fail("carousell", "reply", "message_box")
    client = StubClient()  # only the shipped `textarea` resolves
    _sink(store, bus, client).send(thread, "hi", "reply", _reserve(store))
    typed = [args for name, args in client.calls if name == "browser_type"]
    assert typed[0]["target"] == "textarea"


def test_a_cache_row_that_no_longer_resolves_falls_back_and_is_counted(store, bus, thread) -> None:
    """The cache is an accelerator: a heal that has gone bad costs one extra locate, not a send."""
    store.ui_cache_record(
        market="carousell",
        flow="reply",
        step="message_box",
        strategy="css",
        query="div.gone",
        page_url_pattern="/inbox/",
        action_kind="type",
    )
    client = StubClient()  # div.gone matches nothing; the shipped textarea does
    _sink(store, bus, client).send(thread, "hi", "reply", _reserve(store))
    typed = [args for name, args in client.calls if name == "browser_type"]
    assert typed[0]["target"] == "textarea"
    assert store.ui_cache_get("carousell", "reply", "message_box")["selector"]["fail_count"] == 1


def test_a_total_miss_counts_the_cache_row_and_emits_a_heal_event(store, bus, thread) -> None:
    store.ui_cache_record(
        market="carousell",
        flow="reply",
        step="message_box",
        strategy="css",
        query="div.gone",
        page_url_pattern="/inbox/",
        action_kind="type",
    )
    client = StubClient(matches={})  # nothing resolves at all
    with pytest.raises(sink.SendNotAttempted):
        _sink(store, bus, client).send(thread, "hi", "reply", _reserve(store))
    heals = [e.payload for e in _events(bus, "browser.heal")]
    assert {h["source"] for h in heals} == {"cache", "shipped"}
    assert all(h["outcome"] == "miss" for h in heals)


def test_a_selector_on_the_wrong_page_is_a_miss(store, bus, thread) -> None:
    """The page guard is what stops a selector resolving against whatever happened to be open."""
    client = StubClient()
    client.url = "https://www.carousell.sg/p/some-listing/"

    def navigate(url):
        client.calls.append(("navigate", url))  # deliberately does not change the page

    client.navigate = navigate
    with pytest.raises(sink.SendNotAttempted):
        _sink(store, bus, client).send(thread, "hi", "reply", _reserve(store))


# --- the selector helpers -----------------------------------------------------------------------


def test_only_actionable_strategies_become_targets() -> None:
    assert selectors.as_target("css", "textarea") == "textarea"
    assert selectors.as_target("aria", "Send") == '[aria-label="Send"]'
    assert selectors.as_target("role", "button") == '[role="button"]'
    # a text match can locate something for a human, but no input verb can be aimed at it
    assert selectors.as_target("text", "Send") is None


def test_a_text_strategy_row_is_not_offered_as_a_candidate(store) -> None:
    store.ui_cache_record(
        market="carousell",
        flow="reply",
        step="message_box",
        strategy="text",
        query="Type a message",
        page_url_pattern="/inbox/",
        action_kind="type",
    )
    shipped = market_adapters.CAROUSELL.composer_step("message_box")
    found = selectors.candidates(store, "carousell", "reply", "message_box", shipped)
    assert [c.source for c in found] == ["shipped"]


def test_the_locate_probe_is_read_only() -> None:
    js = selectors.locate_js("textarea")
    assert "querySelectorAll" in js and "getBoundingClientRect" in js
    for mutation in (".value", "click()", "dispatchEvent", "innerHTML"):
        assert mutation not in js


def test_usable_requires_one_match_on_the_right_page() -> None:
    assert selectors.usable({"matches": 1, "url": "https://x/inbox/9/"}, "/inbox/") is True
    assert selectors.usable({"matches": 0, "url": "https://x/inbox/9/"}, "/inbox/") is False
    assert selectors.usable({"matches": 2, "url": "https://x/inbox/9/"}, "/inbox/") is False
    assert selectors.usable({"matches": 1, "url": "https://x/p/thing/"}, "/inbox/") is False
