"""The registration send: a Craigslist reply through bazaar's send_registration_reply, retried
under the send intent's id as the relay send is."""

from __future__ import annotations

from sellee.rail.client import RailError, RailToolRefused
from sellee.rail.sink import RelayReplySink, is_refusal

# bazaar's Unavailable: nothing was sent, and the same id may be tried again.
_NOT_SENT_TEXT = "the reply was not sent"


def reply_subject(subject: str) -> str:
    """`Re:` and the buyer's subject on one line, as bazaar accepts only a one-line subject."""
    line = " ".join((subject or "").split())
    if not line:
        return "Re: your message"
    return line if line.lower().startswith("re:") else f"Re: {line}"


def is_registration_refusal(exc: RailError) -> bool:
    if isinstance(exc, RailToolRefused) and str(exc).strip().lower().startswith(_NOT_SENT_TEXT):
        return False
    return is_refusal(exc)


class RegistrationReplySink(RelayReplySink):
    """A `ReplySink` for market craigslist: mails the thread's counterpart, or raises."""

    _EVENT = "registration.send"

    def _call(self, thread: dict, text: str, intent_id: str) -> dict:
        send_registration_reply(self._client, self._store, thread, text, intent_id)
        # No message id comes back, so the commit names the reply after its intent.
        return {}

    def _is_refusal(self, exc: RailError) -> bool:
        return is_registration_refusal(exc)


def send_registration_reply(client, store, thread: dict, text: str, intent_id: str) -> dict:
    """One send_registration_reply call answering the thread's latest mail."""
    subject = reply_subject(store.latest_registration_subject(thread["thread_id"]))
    return client.send_registration_reply(thread["counterpart_handle"], subject, text, intent_id)
