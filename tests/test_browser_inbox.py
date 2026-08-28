"""The inbox read lane: what it records, what it refuses to guess, and how it behaves when it
cannot see.

The client is a stub rather than the subprocess fake — these tests are about the lane's decisions
(open or skip, adopt or ignore, blind or quiet), and scripting those through a JSON-RPC transport
would obscure them. The transport itself is covered in test_browser_client.py.
"""

from __future__ import annotations

import pytest

from sellee.browser import inbox, reconcile
from sellee.browser.client import BrowserDetached, BrowserToolError, BrowserUnavailable
from sellee.browser.markets import carousell as carousell_market
from sellee.channel import fastpaths
from sellee.config import Config

_INBOX = "https://www.carousell.sg/inbox/"
_LISTING = "https://www.carousell.sg/p/teak-lamp-1328307791/"
_PRODUCT_ID = "1328307791"


class StubClient:
    """A browser answering each of the adapter's artifacts from a script."""

    def __init__(self, *, login="logged_in", conversations=(), tails=None, fail=None, error=None):
        self.login = login
        self.conversations = conversations
        self.error = error
        self.tails = tails or {}
        self.fail = fail
        self.navigations: list = []
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
        if self.fail == "navigate":
            raise BrowserToolError("navigation refused")
        if self.fail == "detached":
            raise BrowserDetached("the browser server lost its connection to Chrome")
        self.navigations.append(url)
        self.url = url

    def evaluate(self, function, **kwargs):
        # Dispatch on the adapter's own artifacts, so a change to one shows up here as a missing
        # case rather than as a substring match landing on the wrong branch.
        if function == carousell_market.LOGIN_JS:
            return {"state": self.login}
        if function == carousell_market.CONVERSATIONS_LIST_JS:
            if self.error is not None:
                return {"error": self.error}
            return {"conversations": list(self.conversations)}
        if function == carousell_market.CONVERSATION_TAIL_JS:
            native = self.url.rstrip("/").rsplit("/", 1)[-1]
            tail = self.tails.get(native, [])
            # Two abstain shapes, and both have to reach the lane unchanged rather than becoming an
            # empty read: a bare None, and the mapping a reader uses to say what it measured.
            if tail is None or isinstance(tail, dict):
                return tail
            return list(tail)
        raise AssertionError(f"the lane evaluated an artifact this stub does not know: {function}")


def _deps(store, bus, client, **overrides):
    clock = {"t": 1000.0}

    def now():
        clock["t"] += 1.0
        return clock["t"]

    return inbox.InboxDeps(
        store=store,
        bus=bus,
        config=Config(**overrides) if overrides else Config(),
        browser_factory=lambda: client,
        now=now,
    )


@pytest.fixture
def seeded(store):
    """An item published to carousell, so a conversation about it is recognisable by listing id."""
    store.set_seller_config_section("basics", {"region": "SG"})
    item = store.create_item(title="Teak lamp", list_price=80.0, currency="SGD")
    store.record_listing_url(item["id"], "carousell", _LISTING)
    return store.get_item(item["id"])


def _thread(store, item, tid="carousell:99", handle="bob"):
    store.create_thread(
        thread_id=tid,
        side="sell",
        market="carousell",
        counterpart_handle=handle,
        item_id=item["id"],
    )
    return tid


def _conv(**overrides):
    """One conversation as the marketplace's API reports it."""
    row = {
        "thread_id": "99",
        "handle": "bob",
        "product_id": _PRODUCT_ID,
        "title": "Teak lamp",
        "unread": 1,
        "last_message": "still available?",
        "offer_type": "received",
    }
    row.update(overrides)
    return row


def _bubble(text, side="in"):
    return {"text": text, "side": side, "y": 0}


def _kinds(bus, kind):
    return bus.store.read(kinds=[kind])


def _texts(store):
    return [notice["text"] for notice in store.claim_queued_notices(10)]


def _notices(store):
    return list(store.claim_queued_notices(10))


# --- the happy path -----------------------------------------------------------------------------


def test_a_buyer_message_lands_as_a_row_with_its_verdict(store, bus, seeded) -> None:
    _thread(store, seeded)
    client = StubClient(conversations=[_conv()], tails={"99": [_bubble("still available?")]})
    inbox.inbox_lane(_deps(store, bus, client))

    messages = store.get_thread("carousell:99")["messages"]
    assert [(m["dir"], m["text"]) for m in messages] == [("in", "still available?")]
    assert messages[0]["scam_verdict"] == "clean"
    assert messages[0]["source"] == "marketplace"


def test_the_read_navigates_only_recorded_urls(store, bus, seeded) -> None:
    """Navigation targets come from the registry, never from a remembered or composed URL."""
    _thread(store, seeded)
    client = StubClient(conversations=[_conv()], tails={"99": [_bubble("hi")]})
    inbox.inbox_lane(_deps(store, bus, client))
    assert client.navigations == [_INBOX, "https://www.carousell.sg/inbox/99/"]


def test_a_scam_message_is_stamped_before_any_model_sees_it(store, bus, seeded) -> None:
    _thread(store, seeded)
    text = (
        "I'll arrange the courier — click the link below to receive the money: http://payout.site"
    )
    client = StubClient(conversations=[_conv()], tails={"99": [_bubble(text)]})
    inbox.inbox_lane(_deps(store, bus, client))
    assert store.get_thread("carousell:99")["messages"][0]["scam_verdict"] == "scam"


def test_re_reading_records_nothing_new(store, bus, seeded) -> None:
    _thread(store, seeded)
    client = StubClient(conversations=[_conv()], tails={"99": [_bubble("hi")]})
    deps = _deps(store, bus, client, inbox_full_sweep_every=1)
    inbox.inbox_lane(deps)
    inbox.inbox_lane(deps)
    assert store.get_thread("carousell:99")["message_count"] == 1


def test_reading_never_advances_the_reply_cursor(store, bus, seeded) -> None:
    """Only a committed reply advances it, so a crash between reading and answering leaves the buyer
    eligible rather than silently handled."""
    _thread(store, seeded)
    client = StubClient(conversations=[_conv()], tails={"99": [_bubble("hi")]})
    inbox.inbox_lane(_deps(store, bus, client))
    assert store.get_thread("carousell:99")["cursor_last_msg_id"] is None
    assert [t["thread_id"] for t in store.threads_with_unhandled_inbound()] == ["carousell:99"]


def test_a_manual_seller_reply_is_recorded_and_stops_the_reply_lane(store, bus, seeded) -> None:
    _thread(store, seeded)
    client = StubClient(
        conversations=[_conv()],
        tails={"99": [_bubble("hi"), _bubble("posting today", "out")]},
    )
    inbox.inbox_lane(_deps(store, bus, client))
    assert [m["dir"] for m in store.get_thread("carousell:99")["messages"]] == ["in", "out"]
    assert store.threads_with_unhandled_inbound() == []  # our account spoke last


# --- the skip gate ------------------------------------------------------------------------------


def test_a_thread_whose_last_message_we_already_hold_is_not_opened(store, bus, seeded) -> None:
    _thread(store, seeded)
    store.record_inbound("carousell:99", msg_id="m1", text="still available?", ts=10.0)
    client = StubClient(conversations=[_conv(unread=0)])
    inbox.inbox_lane(_deps(store, bus, client, inbox_full_sweep_every=99))
    assert client.navigations == [_INBOX]  # the thread page was never opened


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (_conv(unread=2), "anything unread"),
        (_conv(unread=0, last_message="a message we do not have"), "an unseen last message"),
    ],
)
def test_the_gate_errs_toward_opening(store, bus, seeded, row, reason) -> None:
    _thread(store, seeded)
    store.record_inbound("carousell:99", msg_id="m1", text="still available?", ts=10.0)
    client = StubClient(conversations=[row], tails={"99": [_bubble("still available?")]})
    inbox.inbox_lane(_deps(store, bus, client, inbox_full_sweep_every=99))
    assert len(client.navigations) == 2, reason


def test_a_never_read_thread_is_always_opened(store, bus, seeded) -> None:
    _thread(store, seeded)
    client = StubClient(conversations=[_conv(unread=0)], tails={"99": [_bubble("hi")]})
    inbox.inbox_lane(_deps(store, bus, client, inbox_full_sweep_every=99))
    assert len(client.navigations) == 2


def test_the_full_sweep_opens_a_thread_the_gate_would_have_skipped(store, bus, seeded) -> None:
    """The gate is a cost optimization, so the sweep bounds its worst case: a stale list costs one
    sweep interval of latency, not a stranded buyer."""
    _thread(store, seeded)
    store.record_inbound("carousell:99", msg_id="m1", text="still available?", ts=10.0)
    client = StubClient(
        conversations=[_conv(unread=0)],
        tails={"99": [_bubble("still available?"), _bubble("hello?")]},
    )
    deps = _deps(store, bus, client, inbox_full_sweep_every=2)
    inbox.inbox_lane(deps)  # tick 1: skipped
    assert store.get_thread("carousell:99")["message_count"] == 1
    inbox.inbox_lane(deps)  # tick 2: swept
    assert [m["text"] for m in store.get_thread("carousell:99")["messages"]] == [
        "still available?",
        "hello?",
    ]


# --- settling our own unconfirmed sends ---------------------------------------------------------
# The 2026-08-27 incident in one section: a checkout link landed, the read-back could not see it,
# the escalation that followed flipped the thread to `escalated`, and `escalated` is the one status
# this lane skips — so the reader that could have answered the question was switched off by asking
# it.


_LINK = "here's your checkout link: https://api.carousell.ai/checkout/abc?listing_id=def"


def _unverified_intent(store, tid="carousell:99", text=_LINK, kind="reply"):
    """An intent the page took and we could not read back, exactly as the sink leaves one."""
    from sellee.engines import pacing

    reserved = store.reserve_reply(
        thread_id=tid,
        kind=kind,
        text=text,
        in_msg_id=None,
        cfg=pacing.resolve(Config(reply_delay_sec=(0, 0)), quiet_hours=(0, 0)),
    )
    store.mark_intent_sent_unverified(reserved["intent_id"])
    return reserved["intent_id"]


def test_an_escalated_thread_is_still_opened_to_chase_our_own_send(store, bus, seeded) -> None:
    _thread(store, seeded)
    # the buyer message our reply answered, already read on an earlier tick
    store.record_inbound("carousell:99", msg_id="in|q|1", text="still available?", ts=500.0)
    intent = _unverified_intent(store)
    store.escalate("carousell:99", open_question="is my message there?", kind="unconfirmed_send")
    assert store.get_thread("carousell:99")["status"] == "escalated"

    client = StubClient(
        conversations=[_conv(unread=0, last_message=_LINK[:40])],
        tails={"99": [_bubble("still available?"), _bubble(_LINK, "out")]},
    )
    inbox.inbox_lane(_deps(store, bus, client))

    assert store.intent_status(intent) == "committed"
    messages = store.get_thread("carousell:99")["messages"]
    assert [(m["dir"], m["text"], m["source"]) for m in messages] == [
        ("in", "still available?", "marketplace"),
        ("out", _LINK, "agent"),
    ]
    assert store.get_thread("carousell:99")["status"] == "active"  # restored, ask withdrawn
    assert store.list_open_escalations() == []
    assert [e.payload["intent_id"] for e in _kinds(bus, "intent.settled")] == [intent]
    assert _notices(store) == []  # the seller hears nothing: this is our own bookkeeping


def test_a_settled_bubble_is_not_also_recorded_as_a_manual_seller_reply(store, bus, seeded) -> None:
    """Settling has to keep the reconciler off the bubble it just recognised, or our own reply is
    journaled as one the seller typed in the app — a `manual` outbound row means "our account spoke
    last" and silences follow-ups on the thread.

    The hard shape: an inbound bubble we have never stored sits BEFORE our settled one, so the
    stored rows share no suffix with the tail's opening and the aligner calls the whole tail new.
    """
    _thread(store, seeded)
    _unverified_intent(store)
    client = StubClient(
        conversations=[_conv(unread=0, last_message=_LINK[:40])],
        tails={"99": [_bubble("wait, still available?"), _bubble(_LINK, "out")]},
    )
    inbox.inbox_lane(_deps(store, bus, client))

    messages = store.get_thread("carousell:99")["messages"]
    assert [(m["dir"], m["source"]) for m in messages] == [
        ("in", "marketplace"),
        ("out", "agent"),
    ]
    assert [m["text"] for m in messages].count(_LINK) == 1


def test_not_finding_the_message_only_counts_the_attempt(store, bus, seeded) -> None:
    """A miss is never a re-send and never a status change — only evidence that we looked, which is
    what the sweep needs before it may ask the seller anything."""
    _thread(store, seeded)
    intent = _unverified_intent(store)
    client = StubClient(
        conversations=[_conv(unread=1, last_message="still available?")],
        tails={"99": [_bubble("still available?")]},
    )
    deps = _deps(store, bus, client)
    inbox.inbox_lane(deps)
    assert [r["verify_attempts"] for r in store.unsettled_intents()] == [1]
    inbox.inbox_lane(deps)
    assert [r["verify_attempts"] for r in store.unsettled_intents()] == [2]
    assert store.intent_status(intent) == "sent_unverified"
    assert _kinds(bus, "intent.settled") == []


def test_a_find_after_the_seller_was_asked_takes_the_question_back(store, bus, seeded) -> None:
    """The case that matters most. Once the sweep has given up and asked, finding the message is
    what lets the ask be withdrawn instead of the seller having to answer it."""
    from sellee import intent_sweep

    _thread(store, seeded)
    intent = _unverified_intent(store)
    intent_sweep.run_stale_intent_sweep(bus=bus, store=store, grace_sec=0, hard_grace_sec=0)
    assert store.intent_status(intent) == "unconfirmed"
    assert len(store.list_open_escalations()) == 1

    client = StubClient(
        conversations=[_conv(unread=0, last_message=_LINK[:40])],
        tails={"99": [_bubble(_LINK, "out")]},
    )
    inbox.inbox_lane(_deps(store, bus, client))

    assert store.intent_status(intent) == "committed"
    assert store.list_open_escalations() == []
    assert store.get_thread("carousell:99")["status"] == "active"


def test_the_read_event_says_how_many_threads_were_opened_to_settle(store, bus, seeded) -> None:
    _thread(store, seeded)
    _unverified_intent(store)
    client = StubClient(
        conversations=[_conv(unread=0, last_message=_LINK[:40])],
        tails={"99": [_bubble(_LINK, "out")]},
    )
    inbox.inbox_lane(_deps(store, bus, client))
    assert [e.payload["settling"] for e in _kinds(bus, "browser.read")] == [1]


# --- new threads --------------------------------------------------------------------------------


def test_a_conversation_about_one_of_our_listings_becomes_a_thread(store, bus, seeded) -> None:
    """The listing is matched by the marketplace's own id, taken out of the URL we recorded when we
    published — an exact join, not a title guess."""
    client = StubClient(
        conversations=[_conv(handle="newbuyer")], tails={"99": [_bubble("is this still there?")]}
    )
    inbox.inbox_lane(_deps(store, bus, client))
    thread = store.get_thread("carousell:99")
    assert thread["counterpart_handle"] == "newbuyer"
    assert thread["item_id"] == seeded["id"] and thread["side"] == "sell"
    assert [m["text"] for m in thread["messages"]] == ["is this still there?"]


@pytest.mark.parametrize(
    ("row", "expected", "reason"),
    [
        (_conv(product_id="999999"), "unknown_listing", "a listing that is not ours"),
        (_conv(product_id=None), "unknown_listing", "a conversation with no listing at all"),
        (_conv(handle=""), "no_handle", "a counterpart we cannot name"),
        (_conv(offer_type="made"), "we_offered", "an offer we made rather than received"),
    ],
)
def test_an_unrecognised_conversation_is_left_alone(store, bus, seeded, row, expected, reason):
    """A thread attached to the wrong item would negotiate against the wrong floor.

    The reason is asserted, not just the refusal: this event is what answers "why is nobody
    answering this buyer", so a single catch-all label would make the ordinary case (someone
    else's listing) read like the alarming one.
    """
    client = StubClient(conversations=[row])
    inbox.inbox_lane(_deps(store, bus, client))
    assert store.list_threads() == [], reason
    assert [e.payload["reason"] for e in _kinds(bus, "browser.unmatched")] == [expected]


def test_two_items_claiming_one_listing_is_reported_as_its_own_problem(store, bus, seeded) -> None:
    """Not "someone else's listing" — our own records disagree, and only saying so gets it fixed."""
    url = store.get_item(seeded["id"])["listing_urls"]["carousell"]
    twin = store.create_item(title="Teak lamp (dupe)", list_price=80.0, currency="SGD")
    store.record_listing_url(twin["id"], "carousell", url)
    client = StubClient(conversations=[_conv()])
    inbox.inbox_lane(_deps(store, bus, client))
    assert store.list_threads() == []
    assert [e.payload["reason"] for e in _kinds(bus, "browser.unmatched")] == ["two_items"]


def test_a_platform_conversation_is_never_a_conversation_to_answer(store, bus, seeded) -> None:
    client = StubClient(conversations=[_conv(handle="carousell_campaigns_sg")])
    inbox.inbox_lane(_deps(store, bus, client))
    assert store.list_threads() == []


def test_a_held_thread_is_not_reopened(store, bus, seeded) -> None:
    """A held thread waits on the seller; reading it would only mark it read on the platform."""
    _thread(store, seeded)
    store.hold_thread("carousell:99", reason="scam")
    client = StubClient(conversations=[_conv()])
    inbox.inbox_lane(_deps(store, bus, client))
    assert client.navigations == [_INBOX]


# --- blind is not quiet -------------------------------------------------------------------------


def test_a_failed_conversation_list_is_counted_not_treated_as_empty(store, bus, seeded) -> None:
    """The list fails with a reason, which is the whole point of asking the marketplace rather than
    reading a page: nothing found cannot be mistaken for nothing there."""
    deps = _deps(store, bus, StubClient(error="HTTP 503"), browser_blind_after=2)
    inbox.inbox_lane(deps)
    assert deps.blind["carousell"] == 1
    assert store.count_queued_notices() == 0  # one failure is not yet blindness
    inbox.inbox_lane(deps)
    assert [e.payload["failures"] for e in _kinds(bus, "browser.blind")] == [1, 2]
    assert "HTTP 503" in _kinds(bus, "browser.blind")[0].payload["reason"]
    assert store.count_queued_notices() == 1


def test_the_blind_notice_is_raised_once_not_every_tick(store, bus, seeded) -> None:
    deps = _deps(store, bus, StubClient(error="boom"), browser_blind_after=1)
    for _ in range(4):
        inbox.inbox_lane(deps)
    assert store.count_queued_notices() == 1


def test_a_successful_read_clears_the_blind_state(store, bus, seeded) -> None:
    client = StubClient(error="boom")
    deps = _deps(store, bus, client, browser_blind_after=5)
    inbox.inbox_lane(deps)
    client.error = None
    inbox.inbox_lane(deps)
    assert "carousell" not in deps.blind


def test_a_browser_error_mid_read_counts_as_blind(store, bus, seeded) -> None:
    deps = _deps(store, bus, StubClient(fail="navigate"), browser_blind_after=1)
    inbox.inbox_lane(deps)
    assert store.count_queued_notices() == 1


def test_an_unreadable_conversation_is_blind_not_a_quiet_buyer(store, bus, seeded) -> None:
    """The tail reader returns null when it cannot find the message list. Treating that as "nothing
    new" is how a buyer gets stranded."""
    _thread(store, seeded)
    client = StubClient(conversations=[_conv()], tails={"99": None})
    deps = _deps(store, bus, client, browser_blind_after=1)
    inbox.inbox_lane(deps)
    assert deps.blind["carousell"] == 1
    assert store.count_queued_notices() == 1
    assert [e.payload["unreadable"] for e in _kinds(bus, "browser.read")] == [1]


def test_a_thread_that_had_messages_cannot_suddenly_read_as_empty(store, bus, seeded) -> None:
    """A conversation exists because someone wrote in it. An empty tail on a thread we already have
    messages for means the page changed shape, not that the history vanished."""
    _thread(store, seeded)
    store.record_inbound("carousell:99", msg_id="m1", text="still available?", ts=10.0)
    client = StubClient(conversations=[_conv()], tails={"99": []})
    deps = _deps(store, bus, client, browser_blind_after=1, inbox_full_sweep_every=1)
    inbox.inbox_lane(deps)
    assert deps.blind["carousell"] == 1
    assert store.count_queued_notices() == 1


def test_a_genuinely_new_empty_thread_is_not_blind(store, bus, seeded) -> None:
    """A conversation the list reports with no message of its own, reading as empty, agrees with
    itself. Nothing is being claimed about vanished history."""
    _thread(store, seeded)
    client = StubClient(conversations=[_conv(last_message="")], tails={"99": []})
    deps = _deps(store, bus, client, browser_blind_after=1, inbox_full_sweep_every=1)
    inbox.inbox_lane(deps)
    assert "carousell" not in deps.blind
    assert store.count_queued_notices() == 0


def test_a_tail_that_disagrees_with_the_conversation_list_is_blind(store, bus, seeded) -> None:
    """The list said this conversation's latest message is some text and the page showed none. We
    cannot see what we were just told is there, which is the definition of blind — and reading it as
    "the buyer said nothing" is how a live thread went unanswered with no signal at all."""
    _thread(store, seeded)
    client = StubClient(conversations=[_conv(last_message="Any defects?")], tails={"99": []})
    deps = _deps(store, bus, client, browser_blind_after=1, inbox_full_sweep_every=1)
    inbox.inbox_lane(deps)
    assert deps.blind.get("carousell") == 1
    assert store.count_queued_notices() == 1


def test_a_repeat_past_the_tail_window_is_blind_not_a_quiet_buyer(store, bus, seeded) -> None:
    """When a buyer's last TAIL_BUBBLES bubbles are all textually identical, every trailing-window
    overlap size content-matches trivially, so the aligner always concludes the whole tail is
    already stored — a repeat beyond the window is silently never recorded, and `new_rows` returns
    no fresh rows indistinguishable from the buyer having said nothing. The marketplace's own
    unread count is the backstop: an empty-fresh read on a conversation still reporting unread
    messages is unreadable, not quiet."""
    _thread(store, seeded)
    tail = [_bubble("hi")] * reconcile.TAIL_BUBBLES
    client = StubClient(conversations=[_conv(unread=1)], tails={"99": tail})
    deps = _deps(store, bus, client, browser_blind_after=1)
    inbox.inbox_lane(deps)  # first read: records all TAIL_BUBBLES occurrences of "hi"
    assert len(store.get_thread("carousell:99")["messages"]) == reconcile.TAIL_BUBBLES
    assert "carousell" not in deps.blind

    inbox.inbox_lane(deps)  # buyer sends a 9th, identical "hi" — the visible tail is unchanged
    assert deps.blind.get("carousell") == 1
    assert store.count_queued_notices() == 1
    # still not silently swallowed as "recorded" — the message count did not grow, but it was
    # flagged rather than treated as a quiet buyer
    assert len(store.get_thread("carousell:99")["messages"]) == reconcile.TAIL_BUBBLES


def test_a_repeat_past_the_tail_window_with_no_unread_still_reads_quiet(store, bus, seeded) -> None:
    """The unread count is what makes a uniform-tail repeat detectable. Without it (nothing new
    reported by the marketplace), the read still can't tell a real 9th repeat from a truly quiet
    buyer — this pins that known limit rather than claiming blindness on every read of a
    uniform tail."""
    _thread(store, seeded)
    tail = [_bubble("hi")] * reconcile.TAIL_BUBBLES
    client = StubClient(conversations=[_conv(unread=0, last_message="hi")], tails={"99": tail})
    deps = _deps(store, bus, client, browser_blind_after=1, inbox_full_sweep_every=1)
    inbox.inbox_lane(deps)
    assert "carousell" not in deps.blind

    inbox.inbox_lane(deps)
    assert "carousell" not in deps.blind
    assert store.count_queued_notices() == 0


# --- login and availability ---------------------------------------------------------------------


def test_a_logged_out_market_is_skipped_with_one_notice(store, bus, seeded) -> None:
    _thread(store, seeded)
    client = StubClient(login="logged_out", conversations=[_conv()])
    deps = _deps(store, bus, client)
    inbox.inbox_lane(deps)
    inbox.inbox_lane(deps)
    assert client.navigations == [_INBOX, _INBOX]  # no thread was opened
    assert store.count_queued_notices() == 1
    assert [e.payload["state"] for e in _kinds(bus, "browser.login")] == ["logged_out"] * 2


def test_the_logged_out_notice_offers_a_button_not_a_shell_command(store, bus, seeded) -> None:
    """The notice is read on a phone. It used to name `sellee connect carousell`, which is a shell
    on a desktop the seller may be nowhere near — so the market stayed dead until they sat down at
    it. The way out has to be tappable from where the notice is read."""
    _thread(store, seeded)
    inbox.inbox_lane(_deps(store, bus, StubClient(login="logged_out", conversations=[_conv()])))

    notice = _notices(store)[0]
    assert "sellee connect" not in notice["text"]
    assert "Carousell" in notice["text"]  # the display name, not the raw id it used to paste
    assert notice["controls"] == [[fastpaths.SIGN_IN_LABEL, "carousell:connectmkt"]]


def test_no_blind_notice_ever_sends_the_seller_to_check_chrome(
    store, bus, seeded, container
) -> None:
    """The regression test for the message the seller actually got.

    Reaching the blind counter *proves* Chrome answered its CDP probe on that same tick — the
    factory runs `ensure_chrome` on every acquisition, and a Chrome that is genuinely down raises
    `BrowserUnavailable` into a different notice carrying the command to start it. So "check that
    the agent's Chrome is running and still logged in" is advice for a condition that, when true,
    never produces this notice. On 2026-08-28 it was sent 126 reads into an outage caused by the
    daemon's own subprocess, to a seller whose Chrome was signed in the whole time.

    Asserted under the container fixture because that is where the old copy was worst: with
    `may_launch=False` acquisition is the probe and nothing else, so a closed desktop Chrome is
    always unavailable and never blind.
    """
    _thread(store, seeded)
    blind = _deps(store, bus, StubClient(error="boom"), browser_blind_after=1)
    inbox.inbox_lane(blind)
    text = _texts(store)[-1]
    assert "start-chrome.sh" not in text
    assert "still logged in" not in text
    assert "Carousell" in text  # the display name, not the raw market id


def test_a_server_that_lost_chrome_is_not_the_marketplace_refusing_us(store, bus, seeded) -> None:
    """The two causes get different sentences because they are different facts. A detach is our own
    plumbing and claims nothing about the seller or the marketplace; a refused conversation list is
    the marketplace, and we know the page loaded because its JS answered."""
    _thread(store, seeded)
    detached = _deps(store, bus, StubClient(fail="detached"), browser_blind_after=1)
    inbox.inbox_lane(detached)
    ours = _texts(store)[-1]
    assert "lost my own connection to Chrome" in ours
    assert "nothing for you to restart" in ours
    assert [e.payload["cause"] for e in _kinds(bus, "browser.blind")] == ["plumbing"]


def test_a_flapping_probe_does_not_re_nag(store, bus, seeded) -> None:
    """An `unknown` tick between two `logged_out` ticks must not re-arm the notice.

    This is the shape the seller actually hit: the probe alternated between logged_out and
    unknown for two hours, the guard cleared on every unknown, and they got the same message seven
    times from one daemon that never restarted. `unknown` is "no answer" everywhere else in the
    lane; only a confirmed sign-in ends the condition.
    """
    _thread(store, seeded)
    client = StubClient(login="logged_out", conversations=[_conv()])
    deps = _deps(store, bus, client)

    inbox.inbox_lane(deps)
    client.login = "unknown"
    inbox.inbox_lane(deps)
    client.login = "logged_out"
    inbox.inbox_lane(deps)

    assert store.count_queued_notices() == 1


def test_signing_back_in_re_arms_the_notice(store, bus, seeded) -> None:
    """The guard is not one-shot for the lifetime of the process: once the seller is confirmed
    back in, a later sign-out is news again."""
    _thread(store, seeded)
    client = StubClient(login="logged_out", conversations=[_conv()], tails={"99": []})
    deps = _deps(store, bus, client)

    inbox.inbox_lane(deps)
    client.login = "logged_in"
    inbox.inbox_lane(deps)
    client.login = "logged_out"
    inbox.inbox_lane(deps)

    assert store.count_queued_notices() == 2


def test_an_unknown_login_state_still_reads(store, bus, seeded) -> None:
    """Unknown must never flip a logged-in seller to needs-login, so the read goes ahead."""
    _thread(store, seeded)
    client = StubClient(login="unknown", conversations=[_conv()], tails={"99": [_bubble("hi")]})
    inbox.inbox_lane(_deps(store, bus, client))
    assert store.get_thread("carousell:99")["message_count"] == 1
    assert store.count_queued_notices() == 0


def test_no_browser_degrades_with_one_notice_and_no_crash(store, bus, seeded) -> None:
    def factory():
        raise BrowserUnavailable("npx not found")

    deps = inbox.InboxDeps(store=store, bus=bus, config=Config(), browser_factory=factory)
    inbox.inbox_lane(deps)
    inbox.inbox_lane(deps)
    assert store.count_queued_notices() == 1
    assert _kinds(bus, "browser.unavailable")


def test_a_server_dying_mid_read_is_unavailable_not_blind(store, bus, seeded) -> None:
    """The binary exists but the server never starts — the same absence the factory reports,
    discovered one step later. The seller needs the install hint, not a Chrome check, so it is
    routed to the one unavailable notice (held to one across ticks) and never the blind counter."""

    class DyingClient(StubClient):
        def navigate(self, url):
            raise BrowserUnavailable("the browser server did not start")

    deps = _deps(store, bus, DyingClient())
    inbox.inbox_lane(deps)
    inbox.inbox_lane(deps)
    assert store.count_queued_notices() == 1
    assert _kinds(bus, "browser.unavailable")
    assert not _kinds(bus, "browser.blind")


# --- yielding the browser -----------------------------------------------------------------------


def test_the_lane_yields_while_a_reply_pass_is_in_flight(store, bus, seeded) -> None:
    """A pass holds the tab for minutes where the lane holds it for seconds, so the lane waits."""
    _thread(store, seeded)
    store.record_inbound("carousell:99", msg_id="m1", text="hi", ts=10.0)
    store.enqueue_reply_pass()
    client = StubClient(conversations=[_conv()])
    inbox.inbox_lane(_deps(store, bus, client))
    assert client.navigations == []


def test_the_lane_yields_for_a_browser_market_publish_but_not_a_rail_one(store, bus, seeded):
    """A rail publish never touches Chrome, so it is not a reason to stop reading."""
    _thread(store, seeded)
    store.enqueue_pass("publish", {"item_id": seeded["id"], "market": "carousell-ai"})
    client = StubClient(conversations=[_conv()], tails={"99": [_bubble("hi")]})
    inbox.inbox_lane(_deps(store, bus, client))
    assert client.navigations  # the rail publish did not stop the read

    store.enqueue_pass("publish", {"item_id": seeded["id"], "market": "carousell"})
    client2 = StubClient(conversations=[_conv()])
    inbox.inbox_lane(_deps(store, bus, client2))
    assert client2.navigations == []


def test_a_paused_agent_reads_nothing(store, bus, seeded) -> None:
    store.set_paused(True, source="test")
    client = StubClient(conversations=[_conv()])
    inbox.inbox_lane(_deps(store, bus, client))
    assert client.navigations == []


def test_each_way_a_conversation_can_be_unreadable_names_itself(store, bus, seeded) -> None:
    """`_read_thread` answers None for three different things, and the tick only ever counted them.
    "3 conversation(s) unreadable" every tick for a day says how many and never which or why — and
    the three causes want three different fixes, so each publishes its own reason."""
    _thread(store, seeded)
    client = StubClient(
        conversations=[_conv()],
        tails={"99": {"error": "no_message_list", "panes": 0, "width": 756, "visible": True}},
    )
    deps = _deps(store, bus, client, browser_blind_after=1, inbox_full_sweep_every=1)
    inbox.inbox_lane(deps)

    said = _kinds(bus, "browser.unreadable")
    assert [e.payload["thread_id"] for e in said] == ["carousell:99"]
    # The measurements ride along, because they are what makes the next occurrence diagnosable.
    assert "no_message_list" in said[0].payload["reason"]
    assert "width=756" in said[0].payload["reason"]
    # And it is still blind — the reason is additive, it does not soften rule 1.
    assert deps.blind["carousell"] == 1


def test_an_empty_list_for_a_conversation_that_is_not_says_so(store, bus, seeded) -> None:
    _thread(store, seeded)
    store.record_inbound("carousell:99", msg_id="m1", text="still available?", ts=10.0)
    client = StubClient(conversations=[_conv()], tails={"99": []})
    deps = _deps(store, bus, client, browser_blind_after=1, inbox_full_sweep_every=1)
    inbox.inbox_lane(deps)
    reasons = [e.payload["reason"] for e in _kinds(bus, "browser.unreadable")]
    assert reasons == ["the message list read as empty for a conversation that is not"]


def test_unread_with_nothing_fresh_says_so(store, bus, seeded) -> None:
    _thread(store, seeded)
    tail = [_bubble("hi")] * reconcile.TAIL_BUBBLES
    client = StubClient(conversations=[_conv(unread=1)], tails={"99": tail})
    deps = _deps(store, bus, client, browser_blind_after=1)
    inbox.inbox_lane(deps)  # records the tail
    inbox.inbox_lane(deps)  # the 9th identical "hi" is invisible to the aligner
    reasons = [e.payload["reason"] for e in _kinds(bus, "browser.unreadable")]
    assert reasons == ["the list reports unread messages and the tail holds nothing new"]


def test_a_readable_conversation_says_nothing_about_being_unreadable(store, bus, seeded) -> None:
    """The steady state. This event is a diagnosis, not a heartbeat."""
    _thread(store, seeded)
    client = StubClient(conversations=[_conv()], tails={"99": [_bubble("still available?")]})
    deps = _deps(store, bus, client, inbox_full_sweep_every=1)
    inbox.inbox_lane(deps)
    assert _kinds(bus, "browser.unreadable") == []


def _clock_deps(store, bus, client, clock, **overrides):
    """Deps whose clock the test drives, so a blind gap can be hours without waiting for them."""
    return inbox.InboxDeps(
        store=store,
        bus=bus,
        config=Config(**overrides) if overrides else Config(),
        browser_factory=lambda: client,
        now=lambda: clock["t"],
    )


def test_a_market_that_comes_back_after_a_long_blind_spell_says_so(store, bus, seeded) -> None:
    """The 28-hour shape. The seller was told the inbox was unreadable and nothing ever retracted
    it, so the market looked dead for a day — the retraction is the whole repair, and it carries how
    far back to scroll in the marketplace's own app."""
    _thread(store, seeded)
    clock = {"t": 1000.0}
    broken = StubClient(fail="detached")
    deps = _clock_deps(store, bus, broken, clock, browser_blind_after=1, inbox_full_sweep_every=1)
    inbox.inbox_lane(deps)
    assert "lost my own connection" in _texts(store)[-1]

    clock["t"] += 28 * 3600.0
    deps.browser_factory = lambda: StubClient(
        conversations=[_conv()], tails={"99": [_bubble("still available?")]}
    )
    inbox.inbox_lane(deps)
    back = _texts(store)[-1]
    assert "reading your Carousell inbox again" in back
    assert "about 28 hours" in back
    assert [e.payload["market"] for e in _kinds(bus, "browser.reading_again")] == ["carousell"]


def test_a_tick_that_read_no_conversation_is_not_an_all_clear(store, bus, seeded) -> None:
    """The trap this gate exists for. The conversation list answers fine and every thread we open is
    unreadable — the live state on 2026-08-29, 22 of 22 on a full sweep. Clearing on "the list
    answered" would announce "I'm reading that market again" on a tick that recorded nothing from
    nobody, which is the lane's first rule exactly inverted."""
    _thread(store, seeded)
    clock = {"t": 1000.0}
    client = StubClient(conversations=[_conv()], tails={"99": None})
    deps = _clock_deps(store, bus, client, clock, browser_blind_after=1, inbox_full_sweep_every=1)
    inbox.inbox_lane(deps)
    assert "can't read the conversations in it" in _texts(store)[-1]

    clock["t"] += 28 * 3600.0
    inbox.inbox_lane(deps)  # still 1 of 1 unreadable
    assert _kinds(bus, "browser.reading_again") == []
    assert not any("inbox again" in text for text in _texts(store))
    assert deps.blind["carousell"] == 2  # still counted, still blind — rule 1 intact


def test_a_self_heal_the_seller_was_never_told_about_stays_quiet(store, bus, seeded) -> None:
    """No warning, no all-clear. Below the notice threshold a blip is the daemon's own business, and
    the correct number of messages about our subprocess is zero."""
    _thread(store, seeded)
    clock = {"t": 1000.0}
    deps = _clock_deps(store, bus, StubClient(fail="detached"), clock, browser_blind_after=99)
    inbox.inbox_lane(deps)
    assert store.count_queued_notices() == 0

    clock["t"] += 28 * 3600.0
    deps.browser_factory = lambda: StubClient(
        conversations=[_conv()], tails={"99": [_bubble("hi")]}
    )
    inbox.inbox_lane(deps)
    assert store.count_queued_notices() == 0  # nothing was ever queued, so nothing to retract
    assert _kinds(bus, "browser.reading_again") == []


def test_a_blip_fixed_within_the_half_hour_is_not_worth_a_message(store, bus, seeded) -> None:
    """Warned, then fixed five minutes later. Chasing that with an all-clear is two messages about
    something the seller never noticed."""
    _thread(store, seeded)
    clock = {"t": 1000.0}
    deps = _clock_deps(store, bus, StubClient(fail="detached"), clock, browser_blind_after=1)
    inbox.inbox_lane(deps)
    _texts(store)  # drain the warning

    clock["t"] += 300.0
    deps.browser_factory = lambda: StubClient(
        conversations=[_conv()], tails={"99": [_bubble("hi")]}
    )
    inbox.inbox_lane(deps)
    assert not any("inbox again" in text for text in _texts(store))
