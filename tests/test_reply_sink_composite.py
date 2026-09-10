"""Routing a reply to the transport that can actually reach the buyer.

Until craigslist there was one way to answer a buyer — type into the marketplace's own chat — so the
daemon built one sink and `send_reply` acquired it without asking which market the thread was on.
Craigslist has no chat: its buyers arrive as relay email, and answering them means sending mail. So
the send path has to route.

The routing key is already in hand — `sink.send(thread, ...)` receives the thread and the browser
sink itself reads `thread["market"]` from it — which is why this needs no change to `send_reply` or
to `ctx.reply_sink()`.

The property worth the most here is laziness. `BrowserReplySink` takes its client from
`browser_factory()` at *construction*, and that call starts Chrome if it is not already up. Building
every leaf upfront would therefore mean a craigslist email starting a browser it never touches — on
a machine where the seller may have no Chrome running at all.
"""

from __future__ import annotations

import pytest

from sellee import reply_sink


class _Leaf:
    """A sink that records what it was asked to send."""

    def __init__(self, name: str):
        self.name = name
        self.sent: list = []

    def send(self, thread, text, kind, intent_id) -> None:
        self.sent.append((thread["thread_id"], text, kind, intent_id))


class _Factory:
    """A leaf factory that counts how many times it was called."""

    def __init__(self, name: str):
        self.name = name
        self.calls = 0
        self.leaf = _Leaf(name)

    def __call__(self):
        self.calls += 1
        return self.leaf


def _thread(market: str, local: str = "1"):
    return {"thread_id": f"{market}:{local}", "market": market}


def test_a_send_goes_to_the_transport_for_its_market() -> None:
    mail = _Factory("mail")
    browser = _Factory("browser")
    sink = reply_sink.CompositeReplySink(default_factory=browser, by_market={"craigslist": mail})

    sink.send(_thread("craigslist"), "hello", "reply", "int_1")
    sink.send(_thread("carousell"), "hi", "reply", "int_2")

    assert [row[0] for row in mail.leaf.sent] == ["craigslist:1"]
    assert [row[0] for row in browser.leaf.sent] == ["carousell:1"]


def test_a_market_with_no_transport_of_its_own_uses_the_default() -> None:
    """Every marketplace with an in-page chat answers the same way, so the browser sink stays the
    default rather than being enumerated per market — a new browser market needs no entry here."""
    browser = _Factory("browser")
    sink = reply_sink.CompositeReplySink(default_factory=browser, by_market={})

    sink.send(_thread("fb"), "hi", "reply", "int_1")
    assert [row[0] for row in browser.leaf.sent] == ["fb:1"]


def test_a_mail_send_never_builds_the_browser_transport() -> None:
    """The property this class exists for. `BrowserReplySink` takes its client from
    `browser_factory()` at construction, and that call starts Chrome when nothing is listening — so
    building leaves eagerly would have a craigslist email start a browser it never uses.
    """
    mail = _Factory("mail")
    browser = _Factory("browser")
    sink = reply_sink.CompositeReplySink(default_factory=browser, by_market={"craigslist": mail})

    sink.send(_thread("craigslist"), "hello", "reply", "int_1")

    assert mail.calls == 1
    assert browser.calls == 0, "the browser transport was built for a mail send"


def test_a_transport_is_built_once_and_reused() -> None:
    """A leaf holds a connection — a browser client, a mail session — so rebuilding it per send
    would reacquire it per send."""
    mail = _Factory("mail")
    sink = reply_sink.CompositeReplySink(
        default_factory=_Factory("browser"), by_market={"craigslist": mail}
    )

    for n in range(3):
        sink.send(_thread("craigslist", str(n)), "hello", "reply", f"int_{n}")

    assert mail.calls == 1
    assert len(mail.leaf.sent) == 3


def test_a_failing_transport_raises_through_untouched() -> None:
    """The caller distinguishes "nothing was sent" from "sent, unconfirmed" by which exception it
    catches, so wrapping one here would collapse a distinction the whole send path rests on."""

    class _Refuses:
        def send(self, thread, text, kind, intent_id):
            raise reply_sink.SendNotAttempted("the composer was not there")

    sink = reply_sink.CompositeReplySink(default_factory=lambda: _Refuses(), by_market={})
    with pytest.raises(reply_sink.SendNotAttempted):
        sink.send(_thread("fb"), "hi", "reply", "int_1")


def test_a_thread_with_no_market_is_refused_rather_than_guessed() -> None:
    """A thread that names no market cannot be routed, and picking the default would send a
    craigslist buyer's reply into a browser that has no such conversation open."""
    sink = reply_sink.CompositeReplySink(default_factory=_Factory("browser"), by_market={})
    with pytest.raises(reply_sink.SendNotAttempted):
        sink.send({"thread_id": "orphan", "market": ""}, "hi", "reply", "int_1")


def test_the_default_may_be_absent_when_only_mail_is_configured() -> None:
    """A seller with craigslist and nothing else needs no browser sink at all — and asking for one
    would start Chrome on a machine that never needs it."""
    mail = _Factory("mail")
    sink = reply_sink.CompositeReplySink(default_factory=None, by_market={"craigslist": mail})

    sink.send(_thread("craigslist"), "hello", "reply", "int_1")
    assert len(mail.leaf.sent) == 1

    with pytest.raises(reply_sink.SendNotAttempted):
        sink.send(_thread("fb"), "hi", "reply", "int_2")
