"""The marketplace send: a scripted, verified reply in the seller's own logged-in Chrome.

The legacy version of this was five CLI steps restated across five prompt files, with the model
holding the bracket together across turns. Here the whole thing is one call the daemon makes:
navigate the recorded thread URL, locate the composer, type, click send, stamp the intent, and
confirm by reading our own message back off the page.

Two failure modes are deliberately different, because the safe response to each is opposite:

  * Nothing was sent (the composer could not be located, the page was wrong). The intent stays
    `pending` and the whole thing is safe to retry, so this fails closed *before* the click.
  * Send was clicked and we could not confirm it. The intent stays `sent_unverified` and is never
    re-driven — the sweep escalates it for a human to look at, because the one thing worse than an
    unconfirmed message is the same message twice.
"""

from __future__ import annotations

import logging

from selly_agent import marketplaces
from selly_agent.browser import markets as market_adapters
from selly_agent.browser import reconcile, selectors
from selly_agent.browser.client import BrowserError

log = logging.getLogger(__name__)

_MESSAGE_BOX = "message_box"
_SEND_BUTTON = "send_button"


class SinkError(Exception):
    """A send that did not happen. Its message is for our own events, never for the buyer."""


class SendNotAttempted(SinkError):
    """Nothing was typed or clicked, so the intent is still safe to retry."""


class SendUnverified(SinkError):
    """Send was clicked and could not be confirmed. Never retried — verified or escalated."""


class BrowserReplySink:
    """A `ReplySink` driven by a market adapter: `send(thread, text, kind, intent_id)`, raise on
    failure. The exception never reaches the buyer or the model; the caller turns it into a status
    and this emits its own events."""

    def __init__(self, *, client, store, bus, region: str | None = None):
        self._client = client
        self._store = store
        self._bus = bus
        self._region = region

    def send(self, thread: dict, text: str, kind: str, intent_id: str) -> None:
        market = thread["market"]
        adapter = market_adapters.get_adapter(market)
        if adapter is None:
            raise SendNotAttempted(f"no browser adapter for {market!r}")
        url = self._thread_url(thread)
        if url is None:
            raise SendNotAttempted(f"no recorded thread URL for {thread['thread_id']!r}")

        try:
            with self._client.exclusive():
                self._client.navigate(url)
                box = self._locate(market, adapter, _MESSAGE_BOX)
                button = self._locate(market, adapter, _SEND_BUTTON)
                self._client.call_tool(
                    "browser_type",
                    {"target": box.target, "element": "the reply message box", "text": text},
                )
                self._client.call_tool(
                    "browser_click",
                    {"target": button.target, "element": "the send button"},
                )
                # Past this line the buyer may already have the message, so nothing below may retry.
                self._store.mark_intent_sent_unverified(intent_id)
                verified = self._verify(adapter, text)
        except BrowserError as exc:
            # A browser failure before the click leaves nothing sent; the type/click calls are the
            # only ones that could have delivered anything, and a failure in them means the action
            # did not complete. Either way the intent's status decides what happens next.
            self._publish(market, thread, "browser_error", str(exc))
            raise SendNotAttempted(str(exc)) from exc

        if not verified:
            self._publish(market, thread, "unverified", "our message was not on the page")
            raise SendUnverified("send could not be confirmed on the page")
        self._publish(market, thread, "sent", None)

    # --- steps --------------------------------------------------------------------------------

    def _thread_url(self, thread: dict) -> str | None:
        thread_id = thread["thread_id"]
        native = thread_id.split(":", 1)[1] if ":" in thread_id else ""
        if not native:
            return None
        return marketplaces.market_url(thread["market"], "thread", self._region, thread_id=native)

    def _locate(self, market: str, adapter, step: str):
        """Resolve one composer control, or fail closed.

        Tries the live cache row first and the shipped default second, so a marketplace that moved a
        control is covered by whatever a previous heal recorded, and a fresh install is covered by
        what shipped. A miss on both raises before anything is typed.
        """
        flow = market_adapters.REPLY_FLOW
        shipped = adapter.composer_step(step)
        found = None
        for candidate in selectors.candidates(self._store, market, flow, step, shipped):
            result = selectors.probe(self._client, candidate.target)
            if found is None and selectors.usable(result, candidate.page_url_pattern):
                found = candidate
                break
            self._miss(market, flow, step, candidate, result)
        if found is None:
            raise SendNotAttempted(f"could not locate {step} on {market}")
        return found

    def _miss(self, market: str, flow: str, step: str, candidate, result: dict) -> None:
        """Record that a candidate did not resolve.

        A cache row is counted even when the shipped default then works: without that, a heal that
        has gone bad would be probed on every send forever. Three failures retire it, so the row
        stops costing anything without anyone having to invalidate it by hand.
        """
        if candidate.source == "cache":
            self._store.ui_cache_fail(market, flow, step)
        self._bus.publish(
            "browser.heal",
            {
                "market": market,
                "flow": flow,
                "step": step,
                "source": candidate.source,
                "matches": result.get("matches"),
                "outcome": "miss",
            },
        )

    def _verify(self, adapter, text: str) -> bool:
        """Confirm the message is on the page as one of ours.

        "No error from the click" is not success — a validation the page refused, a composer that
        silently cleared, or an overlay that swallowed the click all look like success from the
        outside. Only our own words in an outbound bubble count.
        """
        tail = reconcile.classify_tail(self._client.evaluate(adapter.tail_js) or [])
        wanted = reconcile.normalize(text)
        return any(
            bubble["side"] == "out" and reconcile.normalize(bubble["text"]) == wanted
            for bubble in tail
        )

    def _publish(self, market: str, thread: dict, outcome: str, detail: str | None) -> None:
        payload = {"market": market, "thread_id": thread["thread_id"], "outcome": outcome}
        if detail:
            payload["detail"] = detail[:200]
        self._bus.publish("browser.send", payload)
