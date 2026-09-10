"""The mail transport's rows: whether a mailbox is connected, and what has been read from it.

Every assertion here is a measured failure in reverse. The reasoning lives in
`migrations/data/0019_mail_relay.sql`; these pin the behaviour that reasoning demands.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from sellee import migrations
from sellee.db import Database
from sellee.store import Store

HANDOFF = "you+cl@example.com"
VIEW = "https://mail.google.com/mail/u/0/#search/x"


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as where:
        db = Database(pathlib.Path(where) / "t.db")
        for found in migrations.pending("data", db):
            migrations._apply(db, found)
        yield Store(db)


# --- the mailbox is a probed fact, not a settings string ----------------------------------------


def test_a_mailbox_nobody_has_probed_is_not_connected(store) -> None:
    assert store.mail_transport("craigslist") is None
    assert store.mail_ready("craigslist") is False


def test_every_clause_of_readiness_is_required(store) -> None:
    """Each is one failure: signed out reads nothing; no view means an unscoped read of the
    seller's whole mailbox; no verified handoff means conversations expire unreachable and no sale
    can close."""
    store.record_mail_probe("craigslist", provider="gmail", signed_in=True)
    assert store.mail_ready("craigslist") is False, "no view yet"
    store.record_mail_view("craigslist", VIEW)
    assert store.mail_ready("craigslist") is False, "no handoff yet"
    store.record_mail_handoff("craigslist", HANDOFF, verified=False)
    assert store.mail_ready("craigslist") is False, "handoff not verified"
    store.record_mail_handoff("craigslist", HANDOFF, verified=True)
    assert store.mail_ready("craigslist") is True


def _connected(store) -> None:
    store.record_mail_probe("craigslist", provider="gmail", signed_in=True)
    store.record_mail_view("craigslist", VIEW)
    store.record_mail_handoff("craigslist", HANDOFF, verified=True)


def test_a_signed_out_probe_does_not_demote_the_seller_to_unconfigured(store) -> None:
    """A probe answers "are you signed in". Clearing the view would silently move the seller from
    "sign in again" to "set this up from scratch"."""
    _connected(store)
    store.record_mail_probe("craigslist", provider="gmail", signed_in=False)
    found = store.mail_transport("craigslist")
    assert found["view"] == VIEW
    assert found["handoff_address"] == HANDOFF
    assert store.mail_ready("craigslist") is False


def test_signing_back_in_restores_readiness_without_re_verifying(store) -> None:
    _connected(store)
    store.record_mail_probe("craigslist", provider="gmail", signed_in=False)
    store.record_mail_probe("craigslist", provider="gmail", signed_in=True)
    assert store.mail_ready("craigslist") is True


def test_a_different_handoff_address_starts_its_verification_over(store) -> None:
    """A new address has proved nothing — the provider may not accept its tag, and the first proof
    of an unverified handoff is a buyer who cannot be reached."""
    _connected(store)
    store.record_mail_handoff("craigslist", "other+cl@example.com")
    assert store.mail_ready("craigslist") is False


def test_re_recording_the_same_address_keeps_its_verification(store) -> None:
    """The address is derived, so it gets re-recorded on every connect; that must not demote a
    mailbox that has already made the trip."""
    _connected(store)
    store.record_mail_handoff("craigslist", HANDOFF)
    assert store.mail_ready("craigslist") is True


def test_the_provider_the_probe_found_is_what_is_recorded(store) -> None:
    """Checked rather than assumed: a webmail this transport has never been measured against is
    refused by name, not read wrongly."""
    store.record_mail_probe("craigslist", provider="unknown", signed_in=False)
    assert store.mail_transport("craigslist")["provider"] == "unknown"


# --- conversations ------------------------------------------------------------------------------


def test_the_relay_address_is_overwritten_and_the_permalink_is_not(store) -> None:
    """Craigslist mints a new relay address per view, so only the newest message's is worth
    keeping. The permalink does not rotate and is the only join back to an item."""
    store.upsert_mail_thread(
        provider_thread_id="1a08",
        thread_id="craigslist:1a08",
        market="craigslist",
        relay_address="a@reply.craigslist.org",
        posting_url="https://www.craigslist.org/view/d/x/abc",
    )
    store.upsert_mail_thread(
        provider_thread_id="1a08",
        thread_id="craigslist:1a08",
        market="craigslist",
        relay_address="b@reply.craigslist.org",
    )
    found = store.mail_thread("1a08")
    assert found["relay_address"] == "b@reply.craigslist.org"
    assert found["posting_url"] == "https://www.craigslist.org/view/d/x/abc"


def test_a_new_conversation_starts_on_the_relay_leg(store) -> None:
    store.upsert_mail_thread(
        provider_thread_id="1a08", thread_id="craigslist:1a08", market="craigslist"
    )
    assert store.mail_thread("1a08")["off_relay_ts"] is None


def test_the_leg_is_sticky_once_the_buyer_has_forwarded(store) -> None:
    """This decides whether a payment link may be sent. A later relay-leg message must not move it
    back, or a link goes into the relay and vanishes with no bounce and no error."""
    store.upsert_mail_thread(
        provider_thread_id="1a08", thread_id="craigslist:1a08", market="craigslist"
    )
    store.upsert_mail_thread(
        provider_thread_id="1a08",
        thread_id="craigslist:1a08",
        market="craigslist",
        off_relay=True,
    )
    moved = store.mail_thread("1a08")["off_relay_ts"]
    assert moved is not None
    store.upsert_mail_thread(
        provider_thread_id="1a08",
        thread_id="craigslist:1a08",
        market="craigslist",
        off_relay=False,
    )
    assert store.mail_thread("1a08")["off_relay_ts"] == moved


def test_the_handoff_invitation_is_stamped_once(store) -> None:
    """Repeated every message it reads as a bot and buries the answer the buyer asked for."""
    store.upsert_mail_thread(
        provider_thread_id="1a08", thread_id="craigslist:1a08", market="craigslist"
    )
    store.mark_mail_handoff_sent("1a08", now=100.0)
    store.mark_mail_handoff_sent("1a08", now=200.0)
    assert store.mail_thread("1a08")["handoff_sent_ts"] == 100.0


def test_closing_keeps_the_reason_the_seller_needs(store) -> None:
    """Two endings look identical from outside and mean opposite things: a buyer who opted out made
    a choice, and an address that expired before we answered is our latency."""
    store.upsert_mail_thread(
        provider_thread_id="1a08", thread_id="craigslist:1a08", market="craigslist"
    )
    store.close_mail_thread("1a08", "unreachable", "craigslist retired the address")
    found = store.mail_thread("1a08")
    assert found["state"] == "unreachable"
    assert "retired" in found["closed_reason"]


def test_an_unknown_close_state_is_refused(store) -> None:
    store.upsert_mail_thread(
        provider_thread_id="1a08", thread_id="craigslist:1a08", market="craigslist"
    )
    with pytest.raises(ValueError):
        store.close_mail_thread("1a08", "vanished")


# --- idempotence --------------------------------------------------------------------------------


def test_a_message_folded_once_is_never_folded_again(store) -> None:
    """Without this, `reconcile` finds a new tail row every tick: the buyer appears to repeat
    themselves and the agent answers each repeat."""
    assert store.mail_messages_seen(["m1", "m2"]) == set()
    store.mark_mail_messages_seen("1a08", ["m1"])
    assert store.mail_messages_seen(["m1", "m2"]) == {"m1"}
    store.mark_mail_messages_seen("1a08", ["m1"])
    assert store.mail_messages_seen(["m1"]) == {"m1"}


def test_seen_ignores_empty_ids(store) -> None:
    """A message with no stable id cannot be told apart on the next read, so it is never folded —
    recording one would key idempotence on nothing."""
    store.mark_mail_messages_seen("1a08", ["", None])
    assert store.mail_messages_seen(["", None]) == set()
