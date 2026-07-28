"""The inbox read lane: what it records, what it refuses to guess, and how it behaves when it
cannot see.

The client is a stub rather than the subprocess fake — these tests are about the lane's decisions
(open or skip, adopt or ignore, blind or quiet), and scripting those through a JSON-RPC transport
would obscure them. The transport itself is covered in test_browser_client.py.
"""

from __future__ import annotations

import pytest

from selly_agent.browser import inbox
from selly_agent.browser.client import BrowserToolError, BrowserUnavailable
from selly_agent.browser.markets import carousell as carousell_market
from selly_agent.config import Config

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
        self.navigations.append(url)
        self.url = url

    def evaluate(self, function, **kwargs):
        # Dispatch on the adapter's own artifacts, so a change to one shows up here as a missing
        # case rather than as a substring match landing on the wrong branch.
        if function == carousell_market.LOGIN_JS:
            return {"state": self.login}
        if function == carousell_market.CONVERSATIONS_JS:
            if self.error is not None:
                return {"error": self.error}
            return {"conversations": list(self.conversations)}
        if function == carousell_market.TAIL_JS:
            native = self.url.rstrip("/").rsplit("/", 1)[-1]
            tail = self.tails.get(native, [])
            # None is the artifact's abstain signal ("I could not find the message list") and has
            # to reach the lane as None rather than becoming an empty read.
            return None if tail is None else list(tail)
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
    ("row", "reason"),
    [
        (_conv(product_id="999999"), "a listing that is not ours"),
        (_conv(product_id=None), "a conversation with no listing at all"),
        (_conv(handle=""), "a counterpart we cannot name"),
        (_conv(offer_type="made"), "an offer we made rather than received"),
    ],
)
def test_an_unrecognised_conversation_is_left_alone(store, bus, seeded, row, reason) -> None:
    """A thread attached to the wrong item would negotiate against the wrong floor."""
    client = StubClient(conversations=[row])
    inbox.inbox_lane(_deps(store, bus, client))
    assert store.list_threads() == [], reason


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
    """With nothing recorded yet, an empty tail is simply a conversation we have read nothing from —
    no claim is being made about vanished history."""
    _thread(store, seeded)
    client = StubClient(conversations=[_conv()], tails={"99": []})
    deps = _deps(store, bus, client, browser_blind_after=1, inbox_full_sweep_every=1)
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
