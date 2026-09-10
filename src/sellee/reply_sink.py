"""Routing a reply to the transport that can reach the buyer.

Until craigslist there was one way to answer a buyer — type into the marketplace's own chat — so the
daemon built one sink and `send_reply` acquired it without asking which market the thread was on.
Craigslist has no chat: buyers arrive as relay email, and answering them means sending mail. This is
the seam that routes between the two.

It needs no change to `send_reply` or to `ctx.reply_sink()`, because the routing key is already in
hand: `sink.send(thread, ...)` receives the thread, and `browser/sink.py` already reads
`thread["market"]` out of it to pick an adapter.

**Leaves are built lazily and cached.** `BrowserReplySink` takes its client from
`browser_factory()` at construction, and that call starts Chrome when nothing is listening — so
building every leaf upfront would have a craigslist email start a browser it never touches. A leaf
also holds that connection, so it is built once per market and reused.

The exception types live here rather than being re-declared: the caller's decision to retry turns
entirely on which one it catches, and a second definition of "nothing was sent" is a second chance
to get that backwards.
"""

from __future__ import annotations

import logging

from sellee.browser.sink import SendNotAttempted, SendUnverified, SinkError

__all__ = ["CompositeReplySink", "SendNotAttempted", "SendUnverified", "SinkError"]

log = logging.getLogger(__name__)


class CompositeReplySink:
    """A `ReplySink` that dispatches on the thread's market.

    `by_market` maps a market to a factory for the transport that answers its buyers; every market
    without an entry falls to `default_factory`. That default is the browser sink, so a new
    marketplace with an in-page chat needs no entry — only a market that answers some other way
    does.

    `default_factory` may be None, for a seller who has connected nothing the browser answers: a
    craigslist-only seller needs no Chrome to reply, and asking for one would start it.
    """

    def __init__(self, *, default_factory=None, by_market: dict | None = None):
        self._default_factory = default_factory
        self._by_market = dict(by_market or {})
        self._leaves: dict = {}

    def send(self, thread: dict, text: str, kind: str, intent_id: str) -> None:
        market = str((thread or {}).get("market") or "")
        if not market:
            # Falling back to the default here would send this buyer's reply through a transport
            # that has no conversation with them — a message delivered to the wrong place is worse
            # than one refused, because a refusal is retried and a misdelivery is not noticed.
            raise SendNotAttempted(
                f"thread {(thread or {}).get('thread_id')!r} names no market, so there is no "
                "transport to answer it on"
            )
        self._leaf(market).send(thread, text, kind, intent_id)

    def _leaf(self, market: str):
        """The transport for this market, built on first use and kept."""
        if market in self._leaves:
            return self._leaves[market]
        factory = self._by_market.get(market, self._default_factory)
        if factory is None:
            raise SendNotAttempted(f"no way to send a reply on {market!r} here")
        leaf = factory()
        if leaf is None:
            raise SendNotAttempted(f"the {market!r} send path could not be built")
        self._leaves[market] = leaf
        return leaf
