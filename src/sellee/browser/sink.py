"""The marketplace send: a scripted, verified reply in the seller's own logged-in Chrome.

The whole bracket is one call the daemon makes: navigate the recorded thread URL, locate the
composer, type, commit, stamp the intent, and confirm by reading our own message back off the page.

The text is always typed as real input; **how it is committed is the market's own decision**,
carried on its adapter as `chat_message_submit_js` — supplied, the page submits the message itself;
empty, a real key press does, at the cost of pulling the seller's window forward.

Two failure modes are deliberately different, because the safe response to each is opposite:

  * Nothing was sent (the composer could not be located, the page refused the commit). The intent
    stays `pending` and the whole thing is safe to retry, so this fails closed *before* committing.
  * The commit was accepted and we could not confirm it landed. The intent stays `sent_unverified`
    and is never re-driven — the sweep escalates it for a human, because the one thing worse than an
    unconfirmed message is the same message twice.
"""

from __future__ import annotations

import logging
import time

from sellee import marketplaces
from sellee.browser import markets as market_adapters
from sellee.browser import reconcile, selectors
from sellee.browser.client import BrowserError

log = logging.getLogger(__name__)

_MESSAGE_BOX = "message_box"

_SEND_KEY = "Enter"

# How long the read-back keeps looking for its own bubble, and how often. The window is generous
# against a slow chat round-trip and still short against the pacing delay between two sends, so a
# poll can never be what makes a thread look stalled. Both are only ever spent on a send that has
# not confirmed yet — a bubble already on the page returns on the first read.
_VERIFY_WINDOW_SEC = 6.0
_VERIFY_POLL_SEC = 1.0


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

    def __init__(
        self,
        *,
        client,
        store,
        bus,
        region: str | None = None,
        on_drive=None,
        verify_window_sec: float = _VERIFY_WINDOW_SEC,
    ):
        self._client = client
        self._store = store
        self._bus = bus
        self._region = region
        # Called once as a send starts, for a seller who asked to watch the work — injected rather
        # than read here, so the sink stays free of settings and window policy.
        self._on_drive = on_drive
        # How long the read-back may keep looking. A parameter so a test can spend zero real time on
        # the unverified path, which by definition never finds the bubble; zero means read once.
        self._verify_window_sec = verify_window_sec

    def send(self, thread: dict, text: str, kind: str, intent_id: str) -> None:
        self._announce()
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
                if not adapter.chat_message_submit_js:
                    # A real key event lands only on the active tab, so this market's send needs the
                    # agent's tab brought forward first.
                    self._client.ensure_frontmost(url)
                box = self._locate(market, adapter, _MESSAGE_BOX)
                # Filled in one go rather than typed character by character, so a reply containing a
                # newline cannot commit part-way through itself and send half a message.
                self._client.call_tool(
                    "browser_type",
                    {"target": box.target, "element": "the reply message box", "text": text},
                )
                if not self._commit(adapter, box):
                    # The page did not take it, so nothing was delivered and this is still safe to
                    # retry — the one case a key press could never tell us about.
                    self._publish(market, thread, "refused", "the page did not accept the send")
                    raise SendNotAttempted("the page did not accept the send")
                # Past this line the buyer may already have the message, so nothing below may
                # retry — and nothing below may claim the send did not happen.
                self._store.mark_intent_sent_unverified(intent_id)
                try:
                    verified = self._verify(adapter, text)
                except BrowserError as exc:
                    # The read-back failing says nothing about the send, which the page already
                    # accepted: delivered-or-not-unknown is the unverified case, never a retry.
                    self._publish(market, thread, "unverified", str(exc))
                    raise SendUnverified(str(exc)) from exc
        except BrowserError as exc:
            # Only the steps before the commit can raise to here, so nothing was delivered and
            # the intent's pending status keeps this retry-safe.
            self._publish(market, thread, "browser_error", str(exc))
            raise SendNotAttempted(str(exc)) from exc

        if not verified:
            self._publish(market, thread, "unverified", "our message was not on the page")
            raise SendUnverified("send could not be confirmed on the page")
        self._publish(market, thread, "sent", None)

    # --- steps --------------------------------------------------------------------------------

    def _announce(self) -> None:
        """Let the seller see this one, if they asked to watch. Swallowed whole: a window is a view
        onto the send and never part of it, so nothing here may turn a deliverable reply into a
        failed one."""
        if self._on_drive is None:
            return
        try:
            self._on_drive()
        except Exception:
            log.debug("could not bring the window forward for this send", exc_info=True)

    def _commit(self, adapter, box) -> bool:
        """Send what the composer holds, the way this market's adapter says to.

        Answers whether the page accepted it. A market with its own commit reports that; a key press
        cannot, so the trusted path assumes it landed and leaves the read-back to judge.
        """
        if not adapter.chat_message_submit_js:
            self._client.call_tool("browser_press_key", {"key": _SEND_KEY})
            return True
        answer = self._client.evaluate(
            adapter.chat_message_submit_js, target=box.target, element="the reply message box"
        )
        return bool((answer or {}).get("sent"))

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
        """Confirm the message is on the page as one of ours, re-reading until it shows up.

        "No error from the key press" is not success — a validation the page refused, a composer
        that silently cleared, or a chat that ignored the key because it thought the box was empty
        all look like success from the outside. Only our own words in an outbound bubble count.

        Read more than once, because the chat commits the message to its server and re-renders
        afterwards: a single read taken the instant after the submit is a read of the page as it was
        BEFORE the send. The adapter's own polling cannot cover this — it waits for a tail to become
        non-empty, and after a send the tail is never empty, so it returns the stale one
        immediately. Costs nothing when the bubble is already there, which is the normal case.

        A failed read is not evidence of a failed send, so a `BrowserError` here propagates
        unchanged — the caller turns it into the unverified case, never a retry.
        """
        deadline = time.monotonic() + self._verify_window_sec
        while True:
            tail = reconcile.classify_tail(
                self._client.evaluate(adapter.conversation_tail_js) or []
            )
            if reconcile.contains_outbound(tail, text):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(_VERIFY_POLL_SEC, self._verify_window_sec))

    def _publish(self, market: str, thread: dict, outcome: str, detail: str | None) -> None:
        payload = {"market": market, "thread_id": thread["thread_id"], "outcome": outcome}
        if detail:
            payload["detail"] = detail[:200]
        self._bus.publish("browser.send", payload)
