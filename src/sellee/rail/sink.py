"""The relay send: a reply to a carousell.ai email thread, through bazaar's reply_to_thread.

bazaar stores one reply per `client_message_id` and answers once it is stored; its outbox sends
it from there. The id is the send intent's own, written before the first call, so any failure
short of a refusal is retried under it. A refusal on the first attempt stored nothing, so its
intent is dropped; after an attempt that may have stored the reply, the send is left unverified
for the relay lane to settle once bazaar shows it stored, or to retry under the same id.
"""

from __future__ import annotations

import logging
import time

from sellee.browser.sink import SendUnverified, SinkError
from sellee.rail.client import RailAuthError, RailError, RailNetworkError, RailToolRefused

log = logging.getLogger(__name__)

# Pauses before each retry; one more attempt than pauses.
_RETRY_DELAYS_SEC = (1.0, 2.0, 4.0)

# HTTP 4xx answers that mean "try again", as a gateway in front of bazaar may send.
_TRY_AGAIN = frozenset({408, 429})

# The text bazaar's MCP transport gives a 5xx, which carries no client copy.
_TRANSIENT_TEXT = ("internal server error", "service unavailable", "gateway timeout", "bad gateway")


class SendRefused(SinkError):
    """bazaar refused the reply and stored nothing. Final; the intent is dropped."""


def is_refusal(exc: RailError) -> bool:
    """Whether bazaar answered with a refusal, as opposed to a failure that may have stored it."""
    if isinstance(exc, RailAuthError):
        return True
    if isinstance(exc, RailNetworkError):
        return exc.status is not None and exc.status < 500 and exc.status not in _TRY_AGAIN
    if isinstance(exc, RailToolRefused):
        return not str(exc).strip().lower().startswith(_TRANSIENT_TEXT)
    # A response we could not read is not a refusal: the call may have stored the reply.
    return False


class RelayReplySink:
    """A `ReplySink` for market carousell-ai: `send(thread, text, kind, intent_id)` returns the
    message id bazaar gave the reply, or raises."""

    _EVENT = "relay.send"

    def __init__(self, *, client, store, bus, retry_delays_sec=_RETRY_DELAYS_SEC):
        self._client = client
        self._store = store
        self._bus = bus
        self._retry_delays_sec = tuple(retry_delays_sec)

    def _call(self, thread: dict, text: str, intent_id: str) -> dict:
        native = thread["thread_id"].split(":", 1)[1]
        return {"msg_id": self._client.reply_to_thread(native, text, intent_id)["message_id"]}

    def _is_refusal(self, exc: RailError) -> bool:
        return is_refusal(exc)

    def send(self, thread: dict, text: str, kind: str, intent_id: str) -> dict:
        # The call itself can deliver, so nothing new goes to this buyer until it settles.
        self._store.mark_intent_sent_unverified(intent_id)
        failure: RailError | None = None
        for delay in (0.0, *self._retry_delays_sec):
            time.sleep(delay)
            try:
                result = self._call(thread, text, intent_id)
            except RailError as exc:
                if self._is_refusal(exc) and failure is None:
                    self._store.drop_refused_intent(intent_id)
                    self._publish(thread, "refused", str(exc))
                    raise SendRefused(str(exc)) from exc
                failure = exc
                if self._is_refusal(exc):
                    # An earlier attempt may have stored it, so this is unknown, not refused.
                    break
                log.info("%s failed (%s); retrying under the same id", self._EVENT, exc)
                continue
            self._publish(thread, "sent", None)
            return result
        self._publish(thread, "unverified", str(failure))
        raise SendUnverified(str(failure)) from failure

    def _publish(self, thread: dict, outcome: str, detail: str | None) -> None:
        payload = {"market": thread["market"], "thread_id": thread["thread_id"], "outcome": outcome}
        if detail:
            payload["detail"] = detail[:200]
        self._bus.publish(self._EVENT, payload)
