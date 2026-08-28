"""The scripted marketplace send: what it does, and what it refuses to do twice.

The two failure shapes carry opposite consequences, so they get the most attention here: a send
that never happened must stay retryable, and one that happened but could not be confirmed must
never be re-driven — a buyer getting the same message twice is what this design exists to avoid.
"""

from __future__ import annotations

import json
import re

import pytest

from sellee.browser import markets as market_adapters
from sellee.browser import selectors, sink
from sellee.browser.client import BrowserToolError
from sellee.browser.markets import carousell as carousell_market
from sellee.config import Config

_THREAD_URL = "https://www.carousell.sg/inbox/99/"
_FAST = Config(reply_delay_sec=(0, 0), interactive_reply_delay_sec=(0, 0))


class StubClient:
    """A browser whose composer is present or absent per the test, and whose page remembers what was
    typed so a verify read-back can be real rather than asserted."""

    def __init__(
        self,
        *,
        matches=None,
        sent_bubbles=None,
        fail_on=None,
        echo_on_send=True,
        page_accepts=True,
    ):
        self.matches = {"textarea": 1}
        if matches is not None:
            self.matches = matches
        self.bubbles = list(sent_bubbles or [])
        self.fail_on = fail_on
        self.echo_on_send = echo_on_send
        # Whether the page's own handler takes the message — what the submit reports back.
        self.page_accepts = page_accepts
        self.calls: list = []
        self.typed: str | None = None
        self.url = ""

    def ensure_frontmost(self, url):
        self.calls.append(("ensure_frontmost", url))
        if self.fail_on == "ensure_frontmost":
            raise BrowserToolError("could not bring our own tab forward")

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
        if name == "browser_press_key" and self.echo_on_send and self.typed is not None:
            self.bubbles.append({"text": self.typed, "side": "out", "y": 99})
        return "ok"

    def evaluate(self, function, **kwargs):
        if function == carousell_market.CONVERSATION_TAIL_JS:
            return list(self.bubbles)
        if function == carousell_market.CHAT_MESSAGE_SUBMIT_JS:
            self.calls.append(("submit", kwargs.get("target")))
            if self.fail_on == "submit":
                raise BrowserToolError("evaluate refused")
            if self.page_accepts and self.echo_on_send and self.typed is not None:
                self.bubbles.append({"text": self.typed, "side": "out", "y": 99})
            return {"sent": self.page_accepts, "cleared": self.page_accepts}
        # a locate probe: read the target back out of the probe's own source, so this stub answers
        # the selector the sink actually asked about
        found = re.search(r"querySelectorAll\((\".*?\")\)", function)
        if not found:
            raise AssertionError(f"unexpected evaluate: {function}")
        target = json.loads(found.group(1))
        return {"matches": self.matches.get(target, 0), "url": self.url}


@pytest.fixture
def trusted_market(monkeypatch):
    """Carousell's adapter with its own commit removed, standing in for a market that has not opted
    into an untrusted send. That path is the default, so it needs covering even though the one live
    market does not take it."""
    import dataclasses

    plain = dataclasses.replace(market_adapters.CAROUSELL, chat_message_submit_js="")
    monkeypatch.setattr(market_adapters, "get_adapter", lambda market: plain)
    return plain


def _sink(store, bus, client, *, verify_window_sec: float = 0.0):
    # verify_window_sec=0 means "read the page back exactly once": the unverified path never finds
    # the bubble, and spending the real poll window on every such test would only buy waiting.
    return sink.BrowserReplySink(
        client=client, store=store, bus=bus, region="SG", verify_window_sec=verify_window_sec
    )


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


def _reserve(store, thread_id="carousell:99", text="yes, still available!", kind="reply"):
    from sellee.engines import pacing

    reserved = store.reserve_reply(
        thread_id=thread_id,
        kind=kind,
        text=text,
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


def test_a_send_types_commits_and_verifies_on_the_recorded_thread_url(store, bus, thread) -> None:
    client = StubClient()
    intent = _reserve(store)
    _sink(store, bus, client).send(thread, "yes, still available!", "reply", intent)

    names = [call[0] for call in client.calls]
    assert names[0] == "navigate" and client.calls[0][1] == _THREAD_URL
    assert names.index("browser_type") < names.index("submit")
    assert [e.payload["outcome"] for e in _events(bus, "browser.send")] == ["sent"]


def test_a_market_with_its_own_commit_never_takes_the_sellers_foreground(
    store, bus, thread
) -> None:
    """Selecting the tab is the only thing that interrupts the seller, and it is only needed to
    deliver a real key event. A market that commits from the page needs neither."""
    client = StubClient()
    _sink(store, bus, client).send(thread, "yes, still available!", "reply", _reserve(store))
    names = [call[0] for call in client.calls]
    assert "ensure_frontmost" not in names
    assert "browser_press_key" not in names
    assert "submit" in names


def test_a_send_lets_a_watching_seller_see_it_before_it_navigates(store, bus, thread) -> None:
    """Answering a buyer is the thing a seller who turned watch mode on wants to catch, and the
    window has to be up before the page changes rather than after it has finished."""
    client = StubClient()
    seen: list = []
    sink.BrowserReplySink(
        client=client,
        store=store,
        bus=bus,
        region="SG",
        on_drive=lambda: seen.append(len(client.calls)),
    ).send(thread, "yes, still available!", "reply", _reserve(store))
    assert seen == [0]  # once, before the first browser call


def test_a_window_that_will_not_come_forward_never_costs_the_reply(store, bus, thread) -> None:
    """The window is a view onto the send, never part of it."""
    client = StubClient()

    def _boom():
        raise RuntimeError("no window here")

    sink.BrowserReplySink(client=client, store=store, bus=bus, region="SG", on_drive=_boom).send(
        thread, "yes, still available!", "reply", _reserve(store)
    )
    assert [e.payload["outcome"] for e in _events(bus, "browser.send")] == ["sent"]


def test_the_commit_is_dispatched_onto_the_located_composer(store, bus, thread) -> None:
    client = StubClient()
    _sink(store, bus, client).send(thread, "yes, still available!", "reply", _reserve(store))

    typed = next(args for name, args in client.calls if name == "browser_type")
    assert typed["target"] == "textarea"
    assert "slowly" not in typed  # a filled newline must not commit half a message
    # the same element the composer resolved to, rather than a selector repeated inside the JS
    assert next(target for name, target in client.calls if name == "submit") == "textarea"


def test_the_send_leaves_the_intent_for_the_tool_to_commit(store, bus, thread) -> None:
    """The sink's job ends at "it is on the page"; the transcript row and the cursor advance are the
    tool's second transaction."""
    client = StubClient()
    intent = _reserve(store)
    _sink(store, bus, client).send(thread, "yes, still available!", "reply", intent)
    assert _intent_status(store, intent) == "sent_unverified"
    assert store.get_thread("carousell:99")["messages"] == []


# --- nothing sent: fail closed, stay retryable ---------------------------------------------------


def test_a_composer_miss_fails_before_anything_is_typed(store, bus, thread) -> None:
    """A selector that resolves to nothing must stop the send while it is still safe to retry."""
    client = StubClient(matches={"textarea": 0})
    intent = _reserve(store)
    with pytest.raises(sink.SendNotAttempted, match="message_box"):
        _sink(store, bus, client).send(thread, "hi", "reply", intent)
    assert [name for name, _ in client.calls] == ["navigate"]  # nothing typed, nothing committed
    assert _intent_status(store, intent) == "pending"  # retry-safe


def test_several_matches_is_a_miss_not_a_pick(store, bus, thread) -> None:
    """Choosing one of several would be a silent guess on the account-sensitive path."""
    client = StubClient(matches={"textarea": 3})
    with pytest.raises(sink.SendNotAttempted):
        _sink(store, bus, client).send(thread, "hi", "reply", _reserve(store))


def test_a_browser_failure_while_typing_leaves_the_intent_pending(store, bus, thread) -> None:
    client = StubClient(fail_on="browser_type")
    intent = _reserve(store)
    with pytest.raises(sink.SendNotAttempted):
        _sink(store, bus, client).send(thread, "hi", "reply", intent)
    assert _intent_status(store, intent) == "pending"


def test_a_page_that_refuses_the_commit_leaves_the_intent_pending(store, bus, thread) -> None:
    """The page's own handler did not take it, so nothing was delivered and this is safe to retry —
    the case a key press could never report, because pressing a key always "succeeds"."""
    client = StubClient(page_accepts=False)
    intent = _reserve(store)
    with pytest.raises(sink.SendNotAttempted, match="did not accept"):
        _sink(store, bus, client).send(thread, "hi", "reply", intent)
    assert _intent_status(store, intent) == "pending"
    assert [e.payload["outcome"] for e in _events(bus, "browser.send")] == ["refused"]


def test_a_browser_failure_committing_leaves_the_intent_pending(store, bus, thread) -> None:
    """Text sitting uncommitted in the composer is not a delivered message, so this stays
    retryable — the next attempt navigates afresh and refills the box."""
    client = StubClient(fail_on="submit")
    intent = _reserve(store)
    with pytest.raises(sink.SendNotAttempted):
        _sink(store, bus, client).send(thread, "hi", "reply", intent)
    assert _intent_status(store, intent) == "pending"


# --- the market that has not opted into an untrusted send ----------------------------------------


def test_the_default_commit_is_a_real_key_press_after_taking_the_tab(
    store, bus, thread, trusted_market
):
    """No `chat_message_submit_js` means the market has not opted in, so the send stays a real key
    event — at the cost of bringing the agent's tab forward to deliver it."""
    client = StubClient()
    _sink(store, bus, client).send(thread, "yes, still available!", "reply", _reserve(store))
    names = [name for name, _ in client.calls]
    assert names.index("ensure_frontmost") < names.index("browser_type")
    assert names.index("browser_type") < names.index("browser_press_key")
    assert "submit" not in names


def test_a_tab_that_will_not_come_forward_stops_before_anything_is_typed(
    store, bus, thread, trusted_market
):
    """Keys never reach a background tab, so a send there would fill the box and quietly drop the
    message. Refusing while nothing has been typed keeps the intent retryable."""
    client = StubClient(fail_on="ensure_frontmost")
    intent = _reserve(store)
    with pytest.raises(sink.SendNotAttempted):
        _sink(store, bus, client).send(thread, "hi", "reply", intent)
    assert "browser_type" not in [name for name, _ in client.calls]
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


def test_a_long_reply_verifies_against_its_truncated_bubble(store, bus, thread) -> None:
    """The tail artifact caps bubble text, so a long reply reads back cut short — still ours,
    still confirmed. Demanding full equality would escalate every long reply as unverified."""

    class TruncatingClient(StubClient):
        def evaluate(self, function, **kwargs):
            result = super().evaluate(function, **kwargs)
            if function == carousell_market.CONVERSATION_TAIL_JS:
                return [dict(bubble, text=bubble["text"][:300]) for bubble in result]
            return result

    long_reply = "yes! " + "it comes with the original box and receipts " * 8
    client = TruncatingClient()
    intent = _reserve(store, text=long_reply)
    _sink(store, bus, client).send(thread, long_reply, "reply", intent)
    assert [e.payload["outcome"] for e in _events(bus, "browser.send")] == ["sent"]


def test_the_read_back_keeps_looking_until_the_bubble_renders(store, bus, thread) -> None:
    """The chat commits the message to its server and re-renders afterwards, so the page read the
    instant after the submit is the page as it was BEFORE the send. On 2026-08-27 a checkout link to
    no.202 was reported unverified for exactly this shape of reason; one read is not an answer."""

    class SlowRenderClient(StubClient):
        """Takes the message but only paints it on the third tail read."""

        def __init__(self, **kw):
            super().__init__(echo_on_send=False, **kw)
            self.reads = 0
            self.pending: str | None = None

        def call_tool(self, name, arguments):
            result = super().call_tool(name, arguments)
            if name == "browser_type":
                self.pending = arguments["text"]
            return result

        def evaluate(self, function, **kwargs):
            if function == carousell_market.CONVERSATION_TAIL_JS:
                self.reads += 1
                if self.reads >= 3 and self.pending is not None:
                    self.bubbles.append({"text": self.pending, "side": "out", "y": 99})
                return list(self.bubbles)
            return super().evaluate(function, **kwargs)

    client = SlowRenderClient()
    intent = _reserve(store)
    _sink(store, bus, client, verify_window_sec=5.0).send(thread, "on its way!", "reply", intent)
    assert client.reads >= 3
    assert [e.payload["outcome"] for e in _events(bus, "browser.send")] == ["sent"]


def test_a_checkout_link_bubble_verifies_the_way_the_live_page_renders_it(store, bus, thread):
    """Carousell autolinks a URL into `<a><span>…</span></a>` inside the message's own `<p>`, and
    the reader caps bubble text at 300 characters. Captured from the live DOM on 2026-08-27: the
    bubble
    comes back as one outbound row holding the whole message, cut short. It must verify."""
    link = (
        "All sorted — here's your checkout link: "
        "https://api.carousell.ai/checkout/8a08c727-872d-430c-968e-4978a2cafca1"
        "?listing_id=2313c1ec-da9d-465e-bd89-6f16be050d90 Just tap through to pay securely and "
        "I'll get it packed and shipped to your postal code 😊 (Heads up: this sale is handled by "
        "SELLY for the seller — you'll complete payment and delivery securely at checkout.)"
    )
    assert len(link) > 300, "the point of this test is a reply longer than the reader's cap"

    class LinkBubbleClient(StubClient):
        def evaluate(self, function, **kwargs):
            result = super().evaluate(function, **kwargs)
            if function == carousell_market.CONVERSATION_TAIL_JS:
                return [dict(bubble, text=bubble["text"][:300]) for bubble in result]
            return result

    client = LinkBubbleClient()
    intent = _reserve(store, text=link)
    _sink(store, bus, client).send(thread, link, "reply", intent)
    assert [e.payload["outcome"] for e in _events(bus, "browser.send")] == ["sent"]


def test_a_send_we_cannot_confirm_stays_sent_unverified_and_is_never_resent(store, bus, thread):
    """The one thing worse than an unconfirmed message is the same message twice, so this hands the
    thread to the sweep rather than committing again."""
    client = StubClient(echo_on_send=False)  # the page took it, nothing landed on the page
    intent = _reserve(store)
    with pytest.raises(sink.SendUnverified):
        _sink(store, bus, client).send(thread, "yes, still available!", "reply", intent)
    assert _intent_status(store, intent) == "sent_unverified"
    assert [name for name, _ in client.calls].count("submit") == 1
    assert [e.payload["outcome"] for e in _events(bus, "browser.send")] == ["unverified"]


def test_a_read_back_failure_after_the_commit_is_unverified_never_retryable(store, bus, thread):
    """The page took the message and the confirming read then failed. That is delivered-or-not
    unknown — the unverified case — and must never surface as "not attempted", whose whole meaning
    is that a retry is safe."""

    class BlindAfterSendClient(StubClient):
        def evaluate(self, function, **kwargs):
            if function == carousell_market.CONVERSATION_TAIL_JS:
                raise BrowserToolError("tab crashed during the read-back")
            return super().evaluate(function, **kwargs)

    client = BlindAfterSendClient()
    intent = _reserve(store)
    with pytest.raises(sink.SendUnverified):
        _sink(store, bus, client).send(thread, "yes, still available!", "reply", intent)
    assert _intent_status(store, intent) == "sent_unverified"
    assert [e.payload["outcome"] for e in _events(bus, "browser.send")] == ["unverified"]


def test_the_sweep_escalates_an_unverified_send_without_resending(store, bus, thread) -> None:
    from sellee import intent_sweep

    client = StubClient(echo_on_send=False)
    intent = _reserve(store)
    with pytest.raises(sink.SendUnverified):
        _sink(store, bus, client).send(thread, "hi", "reply", intent)
    for _ in range(intent_sweep.MIN_VERIFY_ATTEMPTS):  # the lane re-read and still could not see it
        store.bump_verify_attempt(intent)
    folded = intent_sweep.run_stale_intent_sweep(bus=bus, store=store, grace_sec=0)
    assert len(folded) == 1 and folded[0]["escalation_new"] is True
    assert _intent_status(store, intent) == "unconfirmed"
    assert store.get_thread("carousell:99")["status"] == "escalated"


def test_the_seller_is_not_asked_before_the_machine_has_looked(store, bus, thread) -> None:
    """The complaint this answers: on 2026-08-27 the seller was asked to go and open the app while
    the agent, which reads that same inbox every five minutes, had not re-checked even once. Elapsed
    time is not effort, so the fold waits for a real attempt — and folds anyway once the hard
    ceiling is reached, because a lane that never runs would otherwise leave a buyer hanging."""
    from sellee import intent_sweep

    client = StubClient(echo_on_send=False)
    intent = _reserve(store)
    with pytest.raises(sink.SendUnverified):
        _sink(store, bus, client).send(thread, "hi", "reply", intent)

    assert intent_sweep.run_stale_intent_sweep(bus=bus, store=store, grace_sec=0) == []
    assert _intent_status(store, intent) == "sent_unverified"
    assert store.get_thread("carousell:99")["status"] == "active"  # never escalated
    assert store.list_open_escalations() == []

    store.bump_verify_attempt(intent)  # one look is not enough either
    assert intent_sweep.run_stale_intent_sweep(bus=bus, store=store, grace_sec=0) == []

    # a lane that cannot run at all never bumps the count, so the ceiling folds it regardless
    folded = intent_sweep.run_stale_intent_sweep(
        bus=bus, store=store, grace_sec=0, hard_grace_sec=0
    )
    assert len(folded) == 1
    assert _intent_status(store, intent) == "unconfirmed"


def test_the_ask_the_seller_finally_sees_is_authored_in_code(store, bus, thread) -> None:
    """A pass must not be able to write this one itself — it is only ever legitimate after the gate
    above, and a model raising it early is what made the agent look like it hadn't tried."""
    from sellee import intent_sweep
    from sellee.store import send as send_store

    client = StubClient(echo_on_send=False)
    intent = _reserve(store)
    with pytest.raises(sink.SendUnverified):
        _sink(store, bus, client).send(thread, "hi", "reply", intent)
    intent_sweep.run_stale_intent_sweep(bus=bus, store=store, grace_sec=0, hard_grace_sec=0)

    escalation = store.list_open_escalations()[0]
    assert escalation["kind"] == "unconfirmed_send"
    assert escalation["open_question"] == send_store.UNCONFIRMED_SEND_ASK
    assert escalation["options"] == list(send_store.UNCONFIRMED_SEND_OPTIONS)


# --- settling an unconfirmed send off the page --------------------------------------------------


def _unverified(
    store, bus, thread, text="here's your checkout link: https://x.test/c/1", kind="reply"
):
    intent = _reserve(store, text=text, kind=kind)
    with pytest.raises(sink.SendUnverified):
        _sink(store, bus, StubClient(echo_on_send=False)).send(thread, text, kind, intent)
    return intent, text


def test_an_unsettled_send_is_listed_for_a_lane_to_go_and_look(store, bus, thread) -> None:
    intent, text = _unverified(store, bus, thread)
    listed = store.unsettled_intents()
    assert [r["intent_id"] for r in listed] == [intent]
    assert listed[0]["thread_id"] == "carousell:99"
    assert listed[0]["text"] == text  # the lane needs the words to look for
    assert listed[0]["verify_attempts"] == 0


def test_finding_our_own_message_commits_it_and_withdraws_the_ask(store, bus, thread) -> None:
    """The whole point: Sellee looked, found its own message, and cleaned up after itself."""
    from sellee import intent_sweep

    intent, text = _unverified(store, bus, thread)
    intent_sweep.run_stale_intent_sweep(bus=bus, store=store, grace_sec=0, hard_grace_sec=0)
    assert store.get_thread("carousell:99")["status"] == "escalated"
    escalation = store.list_open_escalations()[0]

    settled = store.settle_intent_from_read(intent)
    assert settled["msg_id"] == f"out|{intent}"
    assert settled["escalations_resolved"] == [escalation["id"]]
    assert _intent_status(store, intent) == "committed"
    folded = store.get_thread("carousell:99")
    assert [(m["dir"], m["text"], m["source"]) for m in folded["messages"]] == [
        ("out", text, "agent")
    ]
    assert folded["status"] == "active"  # restored, and no longer escalated
    assert store.list_open_escalations() == []


def test_a_settle_restores_the_status_the_thread_actually_had(store, bus, thread) -> None:
    """`agreed` must not come back as `active`: the reply tool lets a nudge and a fresh negotiation
    through on an active thread, which on a closed deal is re-opening a sale nobody re-opened."""
    # `agreed` is owned by the confirm flows, not update_thread — stamped directly here so the test
    # stays about the restore rather than about reaching the state.
    with store._db.transaction() as conn:
        conn.execute("UPDATE threads SET status = 'agreed' WHERE thread_id = 'carousell:99'")
    intent, _ = _unverified(store, bus, thread)
    from sellee import intent_sweep

    intent_sweep.run_stale_intent_sweep(bus=bus, store=store, grace_sec=0, hard_grace_sec=0)
    assert store.get_thread("carousell:99")["status"] == "escalated"

    store.settle_intent_from_read(intent)
    assert store.get_thread("carousell:99")["status"] == "agreed"


def test_a_settle_leaves_the_thread_escalated_when_another_ask_is_still_open(store, bus, thread):
    """Withdrawing our own bookkeeping question must not answer the seller's."""
    intent, _ = _unverified(store, bus, thread)
    from sellee import intent_sweep

    intent_sweep.run_stale_intent_sweep(bus=bus, store=store, grace_sec=0, hard_grace_sec=0)
    store.resolve_escalation(store.list_open_escalations()[0]["id"], "asked and answered")
    store.escalate("carousell:99", open_question="is it sealed or opened?", kind="question")

    store.settle_intent_from_read(intent)
    assert _intent_status(store, intent) == "committed"
    assert store.get_thread("carousell:99")["status"] == "escalated"
    assert [e["kind"] for e in store.list_open_escalations()] == ["question"]


def test_settling_twice_changes_nothing(store, bus, thread) -> None:
    """The lane re-reads freely, so a second find has to be a no-op rather than a second row."""
    intent, _ = _unverified(store, bus, thread)
    assert store.settle_intent_from_read(intent) is not None
    assert store.settle_intent_from_read(intent) is None
    assert len(store.get_thread("carousell:99")["messages"]) == 1


def test_a_settled_holding_line_still_leaves_the_buyer_unanswered(store, bus, thread) -> None:
    """A holding line answered nothing, so confirming one must not mark the question handled —
    otherwise settling it strands the buyer exactly the way committing one does."""
    store.record_inbound("carousell:99", msg_id="in|1", text="70 can?", ts=10.0)
    intent, _ = _unverified(
        store, bus, thread, text="Let me check and get right back to you!", kind="holding"
    )
    store.settle_intent_from_read(intent)
    assert store.get_thread("carousell:99")["cursor_last_msg_id"] is None


def test_looking_for_a_message_that_is_gone_eventually_stops(store, bus, thread) -> None:
    """A send the seller deleted, or the marketplace removed, will never be found. Without a ceiling
    the lane would force-open that one conversation on every tick for the life of the install."""
    from sellee.store import send as send_store

    intent, _ = _unverified(store, bus, thread)
    for _ in range(send_store.MAX_VERIFY_ATTEMPTS):
        assert [r["intent_id"] for r in store.unsettled_intents()] == [intent]
        store.bump_verify_attempt(intent)
    assert store.unsettled_intents() == []  # stopped looking
    # …and it is left exactly as it was: never committed, never re-sent
    assert _intent_status(store, intent) == "sent_unverified"
    assert store.get_thread("carousell:99")["messages"] == []


def test_a_miss_only_counts_the_attempt(store, bus, thread) -> None:
    intent, _ = _unverified(store, bus, thread)
    assert store.bump_verify_attempt(intent) == 1
    assert store.bump_verify_attempt(intent) == 2
    assert _intent_status(store, intent) == "sent_unverified"  # never re-sent, never committed
    assert store.get_thread("carousell:99")["messages"] == []


def test_an_inbound_bubble_with_our_text_does_not_count_as_verification(store, bus, thread):
    client = StubClient(
        echo_on_send=False, sent_bubbles=[{"text": "same words", "side": "in", "y": 1}]
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
