"""The relay send: a reply to a carousell.ai email thread, through bazaar's reply_to_thread.

bazaar stores one reply per `client_message_id`, and the id sent is the send intent's own, written
before the first call. So a timeout or a 5xx is retried under that id, and bazaar's answer to the
retry is the same message. A refusal (a blocked buyer, an unknown thread) stored nothing, so it is
final and its intent is dropped. A send still failing after its retries may or may not be stored;
it is left unverified for the relay lane to settle once bazaar shows the reply sent.
"""

from __future__ import annotations

import logging
import time

from sellee.browser.sink import SendUnverified, SinkError
from sellee.rail.client import RailError, RailNetworkError, RailToolError

log = logging.getLogger(__name__)

# Pauses before each retry; one more attempt than pauses.
_RETRY_DELAYS_SEC = (1.0, 2.0, 4.0)

# The text bazaar's MCP transport gives a 5xx or a 503, which carry no client copy.
_TRANSIENT_TEXT = ("internal server error", "service unavailable", "gateway timeout", "bad gateway")


class SendRefused(SinkError):
    """bazaar refused the reply and stored nothing. Final; the intent is dropped."""


def _transient(exc: Exception) -> bool:
    if isinstance(exc, RailNetworkError):
        # No status means no answer at all: a timeout or a dropped connection.
        return exc.status is None or exc.status >= 500
    if isinstance(exc, RailToolError):
        return str(exc).strip().lower().startswith(_TRANSIENT_TEXT)
    return False


class RelayReplySink:
    """A `ReplySink` for market carousell-ai: `send(thread, text, kind, intent_id)` returns the
    message id bazaar gave the reply, or raises."""

    def __init__(self, *, client, store, bus, retry_delays_sec=_RETRY_DELAYS_SEC):
        self._client = client
        self._store = store
        self._bus = bus
        self._retry_delays_sec = tuple(retry_delays_sec)

    def send(self, thread: dict, text: str, kind: str, intent_id: str) -> dict:
        native = thread["thread_id"].split(":", 1)[1]
        # The call itself can deliver, so from here the intent is never re-driven by a new send.
        self._store.mark_intent_sent_unverified(intent_id)
        failure: RailError | None = None
        for delay in (0.0, *self._retry_delays_sec):
            time.sleep(delay)
            try:
                result = self._client.reply_to_thread(native, text, intent_id)
            except RailError as exc:
                if not _transient(exc):
                    self._store.drop_refused_intent(intent_id)
                    self._publish(thread, "refused", str(exc))
                    raise SendRefused(str(exc)) from exc
                log.info("relay reply failed (%s); retrying under the same id", exc)
                failure = exc
                continue
            self._publish(thread, "sent", None)
            return {"msg_id": result["message_id"]}
        self._publish(thread, "unverified", str(failure))
        raise SendUnverified(str(failure)) from failure

    def _publish(self, thread: dict, outcome: str, detail: str | None) -> None:
        payload = {"market": thread["market"], "thread_id": thread["thread_id"], "outcome": outcome}
        if detail:
            payload["detail"] = detail[:200]
        self._bus.publish("relay.send", payload)
