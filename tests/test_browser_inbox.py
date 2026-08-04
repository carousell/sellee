"""The inbox read lane: what it records, what it refuses to guess, and how it behaves when it
cannot see.

The client is a stub rather than the subprocess fake — these tests are about the lane's decisions
(open or skip, adopt or ignore, blind or quiet), and scripting those through a JSON-RPC transport
would obscure them. The transport itself is covered in test_browser_client.py.
"""

from __future__ import annotations

import pytest
from tests.conftest import enable_markets, seed_setting

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
        if function == carousell_market.CONVERSATIONS_LIST_JS:
            if self.error is not None:
                return {"error": self.error}
            return {"conversations": list(self.conversations)}
        if function == carousell_market.CONVERSATION_TAIL_JS:
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
    """An item published to carousell, so a conversation about it is recognisable by listing id —
    with carousell enabled, since the lane reads only the markets the seller opted into."""
    enable_markets(store, "carousell")
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


# --- reading only the markets the seller enabled -------------------------------------------------


def _exploding_factory():
    """A browser factory that fails the test if it is called at all — acquiring the browser starts
    Chrome, which is the thing a seller with nothing enabled must never have happen."""

    def factory():
        raise AssertionError("the browser was acquired for a market that is not enabled")

    return factory


def test_nothing_enabled_means_no_browser_at_all(store, bus, seeded) -> None:
    """The exit criterion: no Chrome, no login probe, no notice — and one event saying why."""
    seed_setting(store, "crosslist_markets", [])
    _thread(store, seeded)
    deps = inbox.InboxDeps(
        store=store, bus=bus, config=Config(), browser_factory=_exploding_factory()
    )
    inbox.inbox_lane(deps)

    assert store.count_queued_notices() == 0
    assert not _kinds(bus, "browser.login")
    assert not _kinds(bus, "browser.blind")
    skipped = _kinds(bus, "browser.read_skipped")
    assert [e.payload for e in skipped] == [{"enabled": []}]


def test_a_skipped_tick_is_reported_every_tick(store, bus, seeded) -> None:
    """Blindness is not silence, and neither is having nothing to read: an operator who asks the log
    why nothing is happening must get an answer for the tick they are looking at, not just the
    first one of the outage."""
    seed_setting(store, "crosslist_markets", [])
    deps = inbox.InboxDeps(
        store=store, bus=bus, config=Config(), browser_factory=_exploding_factory()
    )
    inbox.inbox_lane(deps)
    inbox.inbox_lane(deps)
    assert len(_kinds(bus, "browser.read_skipped")) == 2


def test_enabling_a_market_starts_reads_and_disabling_stops_them(store, bus, seeded) -> None:
    _thread(store, seeded)
    conv = [_conv()]
    tails = {"99": [_bubble("still available?")]}

    seed_setting(store, "crosslist_markets", [])
    off = StubClient(conversations=conv, tails=tails)
    inbox.inbox_lane(_deps(store, bus, off))
    assert off.navigations == []

    seed_setting(store, "crosslist_markets", ["carousell"])
    on = StubClient(conversations=conv, tails=tails)
    inbox.inbox_lane(_deps(store, bus, on))
    assert on.navigations  # the next tick reads, with no restart in between

    seed_setting(store, "crosslist_markets", [])
    off_again = StubClient(conversations=conv, tails=tails)
    inbox.inbox_lane(_deps(store, bus, off_again))
    assert off_again.navigations == []


def test_a_market_with_no_regional_site_is_not_read(store, bus, seeded) -> None:
    """`crosslist_markets` filters on the seller's region, so a region the marketplace does not
    serve leaves nothing enabled — and the browser is never acquired to discover that."""
    store.set_seller_config_section("basics", {"region": "US"})  # Carousell has no US site
    seed_setting(store, "crosslist_markets", ["carousell"])
    deps = inbox.InboxDeps(
        store=store, bus=bus, config=Config(), browser_factory=_exploding_factory()
    )
    inbox.inbox_lane(deps)
    assert [e.payload for e in _kinds(bus, "browser.read_skipped")] == [{"enabled": []}]


def test_an_enabled_market_with_no_inbox_url_is_reported_not_skipped_quietly(
    store, bus, seeded, monkeypatch
) -> None:
    """The old behaviour was a `log.warning` and a return, inside a lane whose whole rule is that
    blindness must not look like quiet. Now it is an event, and it is decided before the browser is
    acquired rather than one step after."""
    real_market_url = inbox.marketplaces.market_url

    def no_inbox(market, key, region=None, **fields):
        if key == "inbox":
            return None
        return real_market_url(market, key, region, **fields)

    monkeypatch.setattr(inbox.marketplaces, "market_url", no_inbox)
    deps = inbox.InboxDeps(
        store=store, bus=bus, config=Config(), browser_factory=_exploding_factory()
    )
    inbox.inbox_lane(deps)

    assert [e.payload for e in _kinds(bus, "browser.market_unreadable")] == [
        {"market": "carousell", "reason": "no_inbox_url"}
    ]
    assert [e.payload for e in _kinds(bus, "browser.read_skipped")] == [{"enabled": ["carousell"]}]


def test_an_enabled_market_with_no_adapter_is_reported(store, bus, seeded, monkeypatch) -> None:
    """Unreachable while the setting requires an adapter — kept as the loud version of what used to
    be a bare `continue`, so the day the two disagree it is visible rather than silent."""
    monkeypatch.setattr(inbox.market_adapters, "get_adapter", lambda market: None)
    deps = inbox.InboxDeps(
        store=store, bus=bus, config=Config(), browser_factory=_exploding_factory()
    )
    inbox.inbox_lane(deps)
    assert [e.payload for e in _kinds(bus, "browser.market_unreadable")] == [
        {"market": "carousell", "reason": "no_adapter"}
    ]


# --- the reply lane reads the same predicate -----------------------------------------------------


def test_no_reply_is_claimed_for_a_market_the_seller_disabled(store, bus, seeded) -> None:
    """The other half of one predicate: a buyer message read before the market was disabled must
    not become a reply pass afterwards — that would post where nothing reads the answer."""
    _thread(store, seeded)
    store.record_inbound("carousell:99", msg_id="m1", text="still available?", ts=10.0)
    seed_setting(store, "crosslist_markets", [])

    inbox.reply_lane(store=store, bus=bus)
    assert store.claim_queued_pass() is None

    seed_setting(store, "crosslist_markets", ["carousell"])
    inbox.reply_lane(store=store, bus=bus)
    claimed = store.claim_queued_pass()
    assert claimed is not None
    assert claimed.payload["thread_ids"] == ["carousell:99"]


def test_the_reply_gate_only_ever_names_browser_markets(store, bus, seeded) -> None:
    """A thread on the rail is not the setting's business — `crosslist_markets` names the external
    marketplaces, and carousell.ai is the one every listing goes on regardless."""
    seed_setting(store, "crosslist_markets", [])
    assert "carousell-ai" not in inbox.disabled_markets(store)
    assert inbox.disabled_markets(store) == inbox.marketplaces.browser_markets()

    seed_setting(store, "crosslist_markets", ["carousell"])
    assert "carousell" not in inbox.disabled_markets(store)
