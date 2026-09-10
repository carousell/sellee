"""The two-part connect: signing in to a marketplace, then to the mailbox its buyers email.

The properties here are about *ordering and refusal*, because those are what a half-finished
connect turns into: a marketplace that lists items and answers nobody.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from sellee import connectables, migrations, settings
from sellee.browser import connect as browser_connect
from sellee.db import Database
from sellee.mail import connect as mail_connect
from sellee.mail import gmail
from sellee.store import Store
from sellee.store.browser import CONNECT_MODE_OPEN


class StubBus:
    def __init__(self):
        self.events = []

    def publish(self, name, payload=None):
        self.events.append((name, payload or {}))


class StubClient:
    def __init__(self, answer):
        self._answer = answer
        self.navigated = []

    def exclusive(self):
        return _Null()

    def navigate(self, url):
        self.navigated.append(url)

    def ensure_frontmost(self, url):
        return None

    def evaluate(self, function):
        return dict(self._answer)


class _Null:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


class Cfg:
    chrome_cdp_port = 0


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as where:
        db = Database(pathlib.Path(where) / "t.db")
        for found in migrations.pending("data", db):
            migrations._apply(db, found)
        found = Store(db)
        found.set_seller_config_section("basics", {"region": "US", "currency": "USD"})
        yield found


def _deps(store, client, bus):
    return browser_connect.ConnectDeps(
        store=store, bus=bus, config=Cfg(), browser_factory=lambda: client
    )


def _notices(store) -> list:
    return [row["text"] for row in store.claim_queued_notices(50)]


# --- the address the handoff is derived from ----------------------------------------------------


def test_the_handoff_is_the_mailboxs_own_address() -> None:
    """Derived, not asked: the mailbox that receives the relay mail *is* the address on the
    marketplace account, so there is nothing for the seller to decide or mistype.

    Plain, and that is a correction. An earlier version appended `+cl` so the read scope could be
    one address existing only for craigslist — reverted, because the tag has to *deliver*, and a
    provider that ignores subaddressing turns the invitation into an address nobody receives.
    """
    assert mail_connect.derive_handoff("Jerry.Neo@example.com") == "jerry.neo@example.com"


def test_an_already_tagged_address_is_left_as_it_is() -> None:
    """Not re-tagged into `you+cl+cl@x.com`, which would look right and receive nothing."""
    assert mail_connect.derive_handoff("you+cl@example.com") == "you+cl@example.com"


def test_something_that_is_not_an_address_derives_nothing() -> None:
    for said in ("", None, "not an address", "two@at@signs.com", "no-domain@localhost"):
        assert mail_connect.derive_handoff(said) == ""


# --- the provider is checked, not assumed -------------------------------------------------------


def test_the_provider_is_read_from_what_the_probe_found() -> None:
    """The seller may have signed a different account into that window, and driving a webmail this
    transport has never been measured against would mean guessing at buyers' messages."""
    assert mail_connect.probe_provider({"provider": "gmail"}) == mail_connect.PROVIDER_OK
    assert mail_connect.probe_provider({"provider": "unknown"}) == mail_connect.PROVIDER_WRONG
    assert mail_connect.probe_provider({}) == mail_connect.PROVIDER_WRONG
    assert mail_connect.probe_provider(None) == mail_connect.PROVIDER_WRONG


def test_an_empty_view_is_readable_and_an_error_is_not() -> None:
    """A mailbox with no buyer mail yet is the normal state at connect time; read as a failure it
    would refuse every new seller."""
    assert mail_connect.view_is_readable({"conversations": [], "empty_stated": True}) is True
    assert mail_connect.view_is_readable({"error": "nothing came back"}) is False
    assert mail_connect.view_is_readable(None) is False


# --- the lane serves both kinds of target -------------------------------------------------------


def _connected(store, bus):
    settings.set_now(store, bus, key="connected_markets", raw_value=["craigslist"])


def test_signing_in_to_the_site_chains_the_mailbox(store) -> None:
    """The two sign-ins are one intent: a Craigslist that is signed in with no mailbox lists items
    and answers nobody, and the publish gate holds its ads back until the mailbox is there."""
    bus = StubBus()
    _connected(store, bus)
    store.request_connect("craigslist", CONNECT_MODE_OPEN)

    browser_connect.connect_lane(_deps(store, StubClient({"state": "logged_in"}), bus))

    assert [row["target"] for row in store.pending_connects()] == ["craigslist-mail"]
    assert any("mailbox" in text for text in _notices(store))


def test_a_mailbox_sign_in_records_the_probe(store) -> None:
    bus = StubBus()
    _connected(store, bus)
    store.request_connect("craigslist-mail", CONNECT_MODE_OPEN)

    browser_connect.connect_lane(
        _deps(store, StubClient({"state": "logged_in", "provider": "gmail"}), bus)
    )

    found = store.mail_transport("craigslist")
    assert found["signed_in"] is True
    assert found["provider"] == "gmail"
    # Not ready yet: the view and the handoff are the mail lane's half.
    assert store.mail_ready("craigslist") is False


def test_the_wrong_provider_is_refused_by_name(store) -> None:
    """Decision: Craigslist does not connect without a supported mailbox. A mailbox that reports
    itself permanently unreadable is worse than one that says "not this provider yet"."""
    bus = StubBus()
    _connected(store, bus)
    store.request_connect("craigslist-mail", CONNECT_MODE_OPEN)

    browser_connect.connect_lane(
        _deps(
            store,
            StubClient({"state": "logged_in", "provider": "unknown", "host": "outlook.live.com"}),
            bus,
        )
    )

    said = " ".join(_notices(store))
    assert "outlook.live.com" in said
    assert gmail.PROVIDER.title() in said
    assert store.mail_transport("craigslist")["signed_in"] is False


def test_a_signed_out_mailbox_asks_them_to_sign_in_where_the_window_is(store) -> None:
    bus = StubBus()
    _connected(store, bus)
    store.request_connect("craigslist-mail", CONNECT_MODE_OPEN)

    browser_connect.connect_lane(
        _deps(store, StubClient({"state": "logged_out", "provider": "gmail"}), bus)
    )

    said = " ".join(_notices(store))
    assert "email you" in said
    assert "Chrome" in said
    assert store.mail_transport("craigslist")["signed_in"] is False


def test_a_mailbox_request_for_a_disconnected_market_is_dropped(store) -> None:
    """A mailbox belongs to a market, and it is the market's opt-in that decides whether any of
    this is wanted — so both halves are gated on the same setting."""
    bus = StubBus()
    store.request_connect("craigslist-mail", CONNECT_MODE_OPEN)
    browser_connect.connect_lane(_deps(store, StubClient({"state": "logged_in"}), bus))
    assert store.pending_connects() == []


def test_a_stale_mail_token_is_cleared_rather_than_retried_forever(store) -> None:
    """`connectables.resolve` returns `None` for an unknown id exactly as `get_adapter` did, so
    the lane's existing branch covers a withdrawn mail target unchanged."""
    bus = StubBus()
    settings.set_now(store, bus, key="connected_markets", raw_value=["craigslist"])
    store.request_connect("myspace-mail", CONNECT_MODE_OPEN)
    browser_connect.connect_lane(_deps(store, StubClient({"state": "logged_in"}), bus))
    assert store.pending_connects() == []


def test_a_market_with_its_own_inbox_chains_nothing(store) -> None:
    """Asking a Facebook seller for a mailbox is asking for a credential nothing would ever use."""
    bus = StubBus()
    settings.set_now(store, bus, key="connected_markets", raw_value=["fb"])
    store.request_connect("fb", CONNECT_MODE_OPEN)
    browser_connect.connect_lane(_deps(store, StubClient({"state": "logged_in"}), bus))
    assert store.pending_connects() == []
    assert connectables.mail_target_for("fb") is None
