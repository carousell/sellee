"""The inbox read lane: what it records, what it refuses to guess, and how it behaves when it
cannot see.

The client here is a stub rather than the subprocess fake — these tests are about the lane's
decisions (open or skip, adopt or ignore, blind or quiet), and scripting those through a JSON-RPC
transport would obscure them. The transport itself is covered in test_browser_client.py.
"""

from __future__ import annotations

import pytest

from selly_agent.browser import inbox
from selly_agent.browser.client import BrowserToolError, BrowserUnavailable
from selly_agent.browser.markets import carousell as carousell_market
from selly_agent.config import Config

_REGION = "SG"
_INBOX = "https://www.carousell.sg/inbox/"


class StubClient:
    """A browser that answers each JS artifact from a script keyed by what the lane is reading."""

    def __init__(self, *, login="logged_in", rows=(), tails=None, fail=None):
        self.login = login
        self.rows = rows
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
        # Dispatch on the adapter's own artifacts, so a change to one of them shows up here as a
        # missing case rather than as a substring match landing on the wrong branch.
        if function == carousell_market.LOGIN_JS:
            return {"state": self.login}
        if function == carousell_market.DISCOVERY_JS:
            return list(self.rows) if self.rows is not None else None
        if function == carousell_market.TAIL_JS:
            native = self.url.rstrip("/").rsplit("/", 1)[-1]
            return list(self.tails.get(native, []))
        raise AssertionError(f"the lane evaluated an artifact this stub does not know: {function}")


def _deps(store, bus, client, **overrides):
    config = Config(**overrides) if overrides else Config()
    clock = {"t": 1000.0}

    def now():
        clock["t"] += 1.0
        return clock["t"]

    return inbox.InboxDeps(
        store=store,
        bus=bus,
        config=config,
        browser_factory=lambda: client,
        now=now,
    )


@pytest.fixture
def seeded(store):
    """An item with a live carousell listing and the seller's region on file."""
    store.set_seller_config_section("basics", {"region": _REGION})
    item = store.create_item(title="Teak lamp", list_price=80.0, currency="SGD")
    return item


def _thread(store, item, tid="carousell:99", handle="bob"):
    store.create_thread(
        thread_id=tid,
        side="sell",
        market="carousell",
        counterpart_handle=handle,
        item_id=item["id"],
    )
    return tid


def _row(text, thread_id="99", unread=False):
    return {"thread_id": thread_id, "text": text, "unread": unread}


def _bubble(text, side="in"):
    return {"text": text, "side": side, "y": 0}


def _kinds(bus, kind):
    return bus.store.read(kinds=[kind])


# --- the happy path -----------------------------------------------------------------------------


def test_a_buyer_message_lands_as_a_row_with_its_verdict(store, bus, seeded) -> None:
    _thread(store, seeded)
    client = StubClient(
        rows=[_row("bob 3:18 PM Teak lamp still available?", unread=True)],
        tails={"99": [_bubble("still available?")]},
    )
    inbox.inbox_lane(_deps(store, bus, client))

    messages = store.get_thread("carousell:99")["messages"]
    assert [(m["dir"], m["text"]) for m in messages] == [("in", "still available?")]
    assert messages[0]["scam_verdict"] == "clean"
    assert messages[0]["source"] == "marketplace"


def test_the_read_navigates_only_recorded_urls(store, bus, seeded) -> None:
    """Navigation targets come from the registry, never from a remembered or composed URL."""
    _thread(store, seeded)
    client = StubClient(
        rows=[_row("bob 3:18 PM Teak lamp hi", unread=True)], tails={"99": [_bubble("hi")]}
    )
    inbox.inbox_lane(_deps(store, bus, client))
    assert client.navigations == [_INBOX, "https://www.carousell.sg/inbox/99/"]


def test_a_scam_message_is_stamped_before_any_model_sees_it(store, bus, seeded) -> None:
    _thread(store, seeded)
    text = (
        "I'll arrange the courier — click the link below to receive the money: http://payout.site/x"
    )
    client = StubClient(
        rows=[_row("bob 3:18 PM Teak lamp courier", unread=True)],
        tails={"99": [_bubble(text)]},
    )
    inbox.inbox_lane(_deps(store, bus, client))
    assert store.get_thread("carousell:99")["messages"][0]["scam_verdict"] == "scam"


def test_re_reading_records_nothing_new(store, bus, seeded) -> None:
    _thread(store, seeded)
    client = StubClient(
        rows=[_row("bob 3:18 PM Teak lamp hi", unread=True)], tails={"99": [_bubble("hi")]}
    )
    deps = _deps(store, bus, client, inbox_full_sweep_every=1)
    inbox.inbox_lane(deps)
    inbox.inbox_lane(deps)
    assert store.get_thread("carousell:99")["message_count"] == 1


def test_reading_never_advances_the_reply_cursor(store, bus, seeded) -> None:
    """Only a committed reply advances it, so a crash between reading and answering leaves the buyer
    eligible rather than silently handled."""
    _thread(store, seeded)
    client = StubClient(
        rows=[_row("bob 3:18 PM Teak lamp hi", unread=True)], tails={"99": [_bubble("hi")]}
    )
    inbox.inbox_lane(_deps(store, bus, client))
    thread = store.get_thread("carousell:99")
    assert thread["cursor_last_msg_id"] is None
    assert [t["thread_id"] for t in store.threads_with_unhandled_inbound()] == ["carousell:99"]


def test_a_manual_seller_reply_is_recorded_and_stops_the_reply_lane(store, bus, seeded) -> None:
    _thread(store, seeded)
    client = StubClient(
        rows=[_row("bob 3:18 PM Teak lamp hi", unread=True)],
        tails={"99": [_bubble("hi"), _bubble("posting today", "out")]},
    )
    inbox.inbox_lane(_deps(store, bus, client))
    assert [m["dir"] for m in store.get_thread("carousell:99")["messages"]] == ["in", "out"]
    # our account spoke last, so nothing is pending on us
    assert store.threads_with_unhandled_inbound() == []


# --- the skip gate ------------------------------------------------------------------------------


def test_a_thread_whose_preview_matches_the_last_row_is_not_opened(store, bus, seeded) -> None:
    _thread(store, seeded)
    store.record_inbound("carousell:99", msg_id="m1", text="still available?", ts=10.0)
    client = StubClient(rows=[_row("bob 3:18 PM Teak lamp still available?")])
    inbox.inbox_lane(_deps(store, bus, client, inbox_full_sweep_every=99))
    assert client.navigations == [_INBOX]  # the thread page was never opened


def test_an_unread_row_is_always_opened(store, bus, seeded) -> None:
    _thread(store, seeded)
    store.record_inbound("carousell:99", msg_id="m1", text="still available?", ts=10.0)
    client = StubClient(
        rows=[_row("bob 3:18 PM Teak lamp still available?", unread=True)],
        tails={"99": [_bubble("still available?")]},
    )
    inbox.inbox_lane(_deps(store, bus, client, inbox_full_sweep_every=99))
    assert len(client.navigations) == 2


def test_a_never_read_thread_is_always_opened(store, bus, seeded) -> None:
    _thread(store, seeded)
    client = StubClient(rows=[_row("bob 3:18 PM Teak lamp hi")], tails={"99": [_bubble("hi")]})
    inbox.inbox_lane(_deps(store, bus, client, inbox_full_sweep_every=99))
    assert len(client.navigations) == 2


def test_the_full_sweep_opens_a_thread_a_lying_preview_would_have_skipped(store, bus, seeded):
    """The gate is a cost optimization, so the sweep is what bounds its worst case: a preview that
    never changes costs one sweep interval of latency, not a stranded buyer."""
    _thread(store, seeded)
    store.record_inbound("carousell:99", msg_id="m1", text="still available?", ts=10.0)
    client = StubClient(
        rows=[_row("bob 3:18 PM Teak lamp still available?")],
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


def test_the_gate_can_be_disabled_entirely(store, bus, seeded) -> None:
    _thread(store, seeded)
    store.record_inbound("carousell:99", msg_id="m1", text="hi", ts=10.0)
    client = StubClient(rows=[_row("bob 3:18 PM Teak lamp hi")], tails={"99": [_bubble("hi")]})
    inbox.inbox_lane(_deps(store, bus, client, inbox_full_sweep_every=1))
    assert len(client.navigations) == 2  # every tick is a sweep


# --- new threads --------------------------------------------------------------------------------


def test_a_row_naming_one_listing_becomes_a_thread(store, bus, seeded) -> None:
    client = StubClient(
        rows=[_row("newbuyer 3:18 PM Teak lamp is this still there?", unread=True)],
        tails={"99": [_bubble("is this still there?")]},
    )
    inbox.inbox_lane(_deps(store, bus, client))
    thread = store.get_thread("carousell:99")
    assert thread["counterpart_handle"] == "newbuyer"
    assert thread["item_id"] == seeded["id"] and thread["side"] == "sell"
    assert [m["text"] for m in thread["messages"]] == ["is this still there?"]


def test_an_ambiguous_row_is_ignored_not_adopted(store, bus, seeded) -> None:
    """Attaching a thread to the wrong item would negotiate against the wrong floor."""
    store.create_item(title="Teak lamp shade", list_price=20.0, currency="SGD")
    client = StubClient(rows=[_row("newbuyer 3:18 PM Teak lamp shade hi", unread=True)])
    inbox.inbox_lane(_deps(store, bus, client))
    assert store.get_thread("carousell:99") is None
    assert _kinds(bus, "browser.unmatched")


def test_a_row_with_no_thread_id_is_ignored(store, bus, seeded) -> None:
    """Without an id there is no durable key and no page to open, so there is nothing to adopt."""
    client = StubClient(rows=[_row("newbuyer 3:18 PM Teak lamp hi", thread_id=None, unread=True)])
    inbox.inbox_lane(_deps(store, bus, client))
    assert store.list_threads() == []


def test_a_platform_row_is_never_a_conversation(store, bus, seeded) -> None:
    client = StubClient(
        rows=[_row("carousell_assistant 3:18 PM Teak lamp relist it?", unread=True)]
    )
    inbox.inbox_lane(_deps(store, bus, client))
    assert store.list_threads() == []


def test_a_held_thread_is_not_reopened(store, bus, seeded) -> None:
    """A held thread waits on the seller; reading it would only mark it read on the platform."""
    _thread(store, seeded)
    store.hold_thread("carousell:99", reason="scam")
    client = StubClient(rows=[_row("bob 3:18 PM Teak lamp hi", unread=True)])
    inbox.inbox_lane(_deps(store, bus, client))
    assert client.navigations == [_INBOX]


# --- blind is not quiet -------------------------------------------------------------------------


def test_an_unreadable_inbox_is_counted_not_treated_as_empty(store, bus, seeded) -> None:
    client = StubClient(rows=None)  # the reader abstained
    deps = _deps(store, bus, client, browser_blind_after=2)
    inbox.inbox_lane(deps)
    assert deps.blind["carousell"] == 1
    assert store.count_queued_notices() == 0  # one failure is not yet blindness
    inbox.inbox_lane(deps)
    assert [e.payload["failures"] for e in _kinds(bus, "browser.blind")] == [1, 2]
    assert store.count_queued_notices() == 1


def test_the_blind_notice_is_raised_once_not_every_tick(store, bus, seeded) -> None:
    deps = _deps(store, bus, StubClient(rows=None), browser_blind_after=1)
    for _ in range(4):
        inbox.inbox_lane(deps)
    assert store.count_queued_notices() == 1


def test_a_successful_read_clears_the_blind_state(store, bus, seeded) -> None:
    client = StubClient(rows=None)
    deps = _deps(store, bus, client, browser_blind_after=5)
    inbox.inbox_lane(deps)
    client.rows = []
    inbox.inbox_lane(deps)
    assert "carousell" not in deps.blind


def test_a_browser_error_mid_read_counts_as_blind(store, bus, seeded) -> None:
    deps = _deps(store, bus, StubClient(fail="navigate"), browser_blind_after=1)
    inbox.inbox_lane(deps)
    assert store.count_queued_notices() == 1


# --- login and availability ---------------------------------------------------------------------


def test_a_logged_out_market_is_skipped_with_one_notice(store, bus, seeded) -> None:
    _thread(store, seeded)
    client = StubClient(login="logged_out", rows=[_row("bob 3:18 PM Teak lamp hi", unread=True)])
    deps = _deps(store, bus, client)
    inbox.inbox_lane(deps)
    inbox.inbox_lane(deps)
    assert client.navigations == [_INBOX, _INBOX]  # no thread was opened
    assert store.count_queued_notices() == 1
    assert [e.payload["state"] for e in _kinds(bus, "browser.login")] == [
        "logged_out",
        "logged_out",
    ]


def test_an_unknown_login_state_still_reads(store, bus, seeded) -> None:
    """Unknown must never flip a logged-in seller to needs-login, so the read goes ahead."""
    _thread(store, seeded)
    client = StubClient(
        login="unknown", rows=[_row("bob 3:18 PM x hi", unread=True)], tails={"99": [_bubble("hi")]}
    )
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
    client = StubClient(rows=[_row("bob 3:18 PM Teak lamp hi", unread=True)])
    inbox.inbox_lane(_deps(store, bus, client))
    assert client.navigations == []


def test_the_lane_yields_for_a_browser_market_publish_but_not_a_rail_one(store, bus, seeded):
    """A rail publish never touches Chrome, so it is not a reason to stop reading."""
    _thread(store, seeded)
    store.enqueue_pass("publish", {"item_id": seeded["id"], "market": "carousell-ai"})
    client = StubClient(
        rows=[_row("bob 3:18 PM Teak lamp hi", unread=True)], tails={"99": [_bubble("hi")]}
    )
    inbox.inbox_lane(_deps(store, bus, client))
    assert client.navigations  # the rail publish did not stop the read

    store.enqueue_pass("publish", {"item_id": seeded["id"], "market": "carousell"})
    client2 = StubClient(rows=[_row("bob 3:18 PM Teak lamp hi", unread=True)])
    inbox.inbox_lane(_deps(store, bus, client2))
    assert client2.navigations == []


def test_a_paused_agent_reads_nothing(store, bus, seeded) -> None:
    store.set_paused(True, source="test")
    client = StubClient(rows=[_row("bob 3:18 PM Teak lamp hi", unread=True)])
    inbox.inbox_lane(_deps(store, bus, client))
    assert client.navigations == []
