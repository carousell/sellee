"""The mail send, driven end to end against a stub browser.

What is pinned here is the *order* of the sequence, because every step in it exists because of
something measured and three of them are wrong-answer-shaped:

  * the recipient check, because a reply to a freshly-read `sale.` address mails the seller their
    own answer while the buyer waits, and nothing bounces;
  * the unverified stamp landing **before** the Send click, because the confirmation toast expires
    in about nine seconds and a crash in between must not read as a send that never happened;
  * "Sending..." not being read as "sent", because that reports a buyer answered while the message
    is still cancellable.

And the leg gate, which decides whether a payment link may be sent at all.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from sellee import migrations
from sellee.browser.sink import SendNotAttempted, SendUnverified
from sellee.db import Database
from sellee.mail import gmail, transport
from sellee.store import Store

HANDOFF = "you+cl@example.com"
RELAY_SENDER = "a9f44b33155f3effb2904e25c5042f3d@reply.craigslist.org"


class StubClient:
    """A browser that answers each artifact from a script, and records what it was asked."""

    def __init__(self, *, recipients=None, verdicts=None, fail=None):
        self.calls = []
        self.navigated = []
        self.fresh_tabs = 0
        self._recipients = recipients or {"to": [RELAY_SENDER], "cc": [], "bcc": []}
        self._verdicts = list(verdicts or [{"sent": True, "said": "Message sent"}])
        self._fail = fail or {}

    # the client surface the sink uses
    def exclusive(self):
        return _Null()

    def fresh_tab(self):
        self.fresh_tabs += 1
        return _Null()

    def navigate(self, url):
        self.navigated.append(url)

    def evaluate(self, function):
        name = _which(function)
        self.calls.append(name)
        if name in self._fail:
            return self._fail[name]
        if name == "open_conversation":
            return {"opened": "1a08", "rows": 1}
        if name == "reply_control":
            return {"label": "Reply", "tag": "button", "disabled": False}
        if name == "click_reply":
            return {"clicked": True}
        if name == "recipients":
            return dict(self._recipients)
        if name == "compose_fill":
            return {"filled": True, "chars": 12, "lines": 1}
        if name == "send":
            return {"clicked": True}
        if name == "verify":
            return self._verdicts.pop(0) if self._verdicts else {"unknown": True}
        raise AssertionError(f"unscripted artifact: {name}")


class _Null:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def _which(function: str) -> str:
    """Name the artifact from its body, so the stub does not depend on identity."""
    if "no row for that conversation" in function:
        return "open_conversation"
    if "in the opened conversation'" in function and "nearby" in function:
        return "reply_control"
    if "expected exactly one control named" in function and "Reply" in function:
        return "click_reply"
    if "no recipient fields" in function:
        return "recipients"
    if "did not accept the text" in function:
        return "compose_fill"
    if "expected exactly one control named" in function:
        return "send"
    if 'role="alert"' in function or 'role=\\"alert\\"' in function:
        return "verify"
    return "unknown"


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as where:
        db = Database(pathlib.Path(where) / "t.db")
        for found in migrations.pending("data", db):
            migrations._apply(db, found)
        found = Store(db)
        found.record_mail_probe("craigslist", provider="gmail", signed_in=True)
        found.record_mail_view("craigslist", gmail.search_view(HANDOFF))
        found.record_mail_handoff("craigslist", HANDOFF, verified=True)
        found.upsert_mail_thread(
            provider_thread_id="1a08",
            thread_id="craigslist:1a08",
            market="craigslist",
            relay_address=RELAY_SENDER,
        )
        yield found


def _sink(store, client, config=None):
    return transport.MailReplySink(
        store=store, config=config or _Config(), browser_factory=lambda: client
    )


class _Config:
    carousell_ai_web_base_url = "https://carousell.ai"


def _thread():
    return {"thread_id": "craigslist:1a08", "market": "craigslist"}


# --- the happy path, and its order --------------------------------------------------------------


def test_a_reply_goes_out_in_the_measured_order(store) -> None:
    client = StubClient()
    _sink(store, client).send(_thread(), "Yes, still available.", "reply", "int_1")
    assert client.calls == [
        "open_conversation",
        "reply_control",
        "click_reply",
        "recipients",
        "compose_fill",
        "send",
        "verify",
    ]


def test_the_read_happens_in_a_fresh_tab(store) -> None:
    """The scope guarantee is a property of the tab, not the URL: a reused tab held 34 of the
    seller's personal emails inside a correctly-scoped search."""
    client = StubClient()
    _sink(store, client).send(_thread(), "hi", "reply", "int_1")
    assert client.fresh_tabs == 1


def test_the_intent_is_stamped_unverified_before_the_send_click(store) -> None:
    """The toast expires in about nine seconds, so a crash between the click and the read must not
    look like a send that never happened."""
    stamped_at = {}
    client = StubClient()
    original = store.mark_intent_sent_unverified

    def watched(intent_id):
        stamped_at["calls"] = list(client.calls)
        return original(intent_id)

    store.mark_intent_sent_unverified = watched
    _sink(store, client).send(_thread(), "hi", "reply", "int_1")
    assert "send" not in stamped_at["calls"], "stamped after the click"
    assert "compose_fill" in stamped_at["calls"], "stamped before the body was written"


def test_the_handoff_invitation_is_recorded_as_sent(store) -> None:
    client = StubClient()
    _sink(store, client).send(_thread(), "hi", "reply", "int_1")
    assert store.mail_thread("1a08")["handoff_sent_ts"] is not None


# --- the recipient check ------------------------------------------------------------------------


def test_a_reply_addressed_outside_the_scope_is_refused(store) -> None:
    """A resolved contact, or a freshly-read `sale.` address — which is what a *buyer* writes to in
    order to reach the *seller*, so sending there mails the seller their own answer."""
    client = StubClient(recipients={"to": ["someone@example.com"], "cc": [], "bcc": []})
    with pytest.raises(SendNotAttempted) as caught:
        _sink(store, client).send(_thread(), "hi", "reply", "int_1")
    assert "don't recognise" in str(caught.value)
    assert "send" not in client.calls


def test_a_reply_with_anyone_copied_in_is_refused(store) -> None:
    """Reply-all on a relay thread copies every address craigslist put on the message — both the
    posting address and the conversation address were measured on one."""
    client = StubClient(recipients={"to": [RELAY_SENDER], "cc": [RELAY_SENDER], "bcc": []})
    with pytest.raises(SendNotAttempted) as caught:
        _sink(store, client).send(_thread(), "hi", "reply", "int_1")
    assert "copied in" in str(caught.value)
    assert "send" not in client.calls


def test_a_reply_addressed_to_nobody_is_refused(store) -> None:
    client = StubClient(recipients={"to": [], "cc": [], "bcc": []})
    with pytest.raises(SendNotAttempted):
        _sink(store, client).send(_thread(), "hi", "reply", "int_1")
    assert "send" not in client.calls


def test_the_handoff_address_is_in_scope_as_a_recipient(store) -> None:
    """Once the buyer has moved to the direct leg, the reply goes to an ordinary address — which
    the relay-only guard would have blocked."""
    client = StubClient(recipients={"to": [HANDOFF], "cc": [], "bcc": []})
    _sink(store, client).send(_thread(), "hi", "reply", "int_1")
    assert "send" in client.calls


# --- confirming the send ------------------------------------------------------------------------


def test_sending_is_polled_rather_than_read_as_sent(store) -> None:
    """The first alert says "Sending... Cancel". Read as success it reports a buyer answered while
    the message is still cancellable."""
    client = StubClient(
        verdicts=[
            {"sending": True, "said": "Sending... Cancel"},
            {"sent": True, "said": "Message sent"},
        ]
    )
    _sink(store, client).send(_thread(), "hi", "reply", "int_1")
    assert client.calls.count("verify") == 2


def test_an_unconfirmable_send_is_unverified_and_never_retried(store) -> None:
    """`SendUnverified`, not a failure: the click happened, so the buyer may already have it."""
    client = StubClient(verdicts=[{"unknown": True, "said": ""}])
    with pytest.raises(SendUnverified) as caught:
        _sink(store, client).send(_thread(), "hi", "reply", "int_1")
    assert "couldn't confirm" in str(caught.value)


def test_a_send_control_that_is_not_there_is_not_attempted(store) -> None:
    """Nothing was handed over, so the intent stays retryable rather than becoming unverified."""
    client = StubClient(
        fail={"send": {"error": "expected exactly one control named Send, found 0"}}
    )
    with pytest.raises(SendNotAttempted):
        _sink(store, client).send(_thread(), "hi", "reply", "int_1")


def test_a_body_the_composer_refuses_is_not_sent(store) -> None:
    """A contenteditable that ignored the write is indistinguishable from one that took it, and the
    difference is whether a buyer gets an empty message recorded as an answer."""
    client = StubClient(fail={"compose_fill": {"error": "the reply body did not accept the text"}})
    with pytest.raises(SendNotAttempted):
        _sink(store, client).send(_thread(), "hi", "reply", "int_1")
    assert "send" not in client.calls


# --- the leg gate, which decides whether a payment link may be sent -----------------------------


def test_a_checkout_link_is_refused_on_the_relay_leg(store) -> None:
    """Link-bearing relay mail is commonly dropped, and craigslist's own advice tells buyers never
    to pay through a link a seller sends. Sent anyway, it vanishes with no bounce: the seller
    believes the buyer was asked to pay and the buyer never saw anything.
    """
    client = StubClient()
    with pytest.raises(SendNotAttempted) as caught:
        _sink(store, client).send(
            _thread(), "Pay here: https://carousell.ai/checkout/abc", "reply", "int_1"
        )
    assert "checkout link" in str(caught.value)
    assert client.calls == [], "nothing was opened"


def test_a_checkout_link_goes_out_on_the_direct_leg(store) -> None:
    """Once the buyer has forwarded to the seller's own address, craigslist is not in the path at
    all — so the close is an ordinary checkout link like every other market."""
    store.upsert_mail_thread(
        provider_thread_id="1a08",
        thread_id="craigslist:1a08",
        market="craigslist",
        off_relay=True,
    )
    client = StubClient()
    _sink(store, client).send(
        _thread(), "Pay here: https://carousell.ai/checkout/abc", "reply", "int_1"
    )
    assert "send" in client.calls


def test_the_leg_gate_reads_durable_state_not_the_message(store) -> None:
    """A restart that forgot the leg would put a payment link into the relay. So the gate is the
    stored `off_relay_ts`, re-read on every send."""
    assert transport._link_hostile(store.mail_thread("1a08")) is True
    store.upsert_mail_thread(
        provider_thread_id="1a08",
        thread_id="craigslist:1a08",
        market="craigslist",
        off_relay=True,
    )
    assert transport._link_hostile(store.mail_thread("1a08")) is False


def test_no_configured_checkout_host_refuses_nothing(store) -> None:
    """The honest behaviour when we do not know what a checkout link looks like."""

    class Bare:
        carousell_ai_web_base_url = ""

    client = StubClient()
    _sink(store, client, config=Bare()).send(
        _thread(), "https://carousell.ai/checkout/abc", "reply", "int_1"
    )
    assert "send" in client.calls
