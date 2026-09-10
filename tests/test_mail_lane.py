"""The lane that reads a mailbox, and the lane that finishes a mailbox connect.

Three properties carry most of the weight here, and each is a measured failure:

  * **a fresh tab per read** — Gmail's row cache put 34 of the seller's personal emails inside a
    correctly-scoped search in a reused tab, so the scope guarantee is a property of the tab;
  * **idempotence from a table** — without `mail_relay_seen` the buyer appears to repeat themselves
    every tick and the agent answers each repeat;
  * **the scam pre-scan** — `tools/reply.py` gates a send on `scam_verdict` being on the row, so a
    read path that skips it does not fail loudly, it silently opens the gate.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from sellee import migrations, settings
from sellee.db import Database
from sellee.mail import lane as mail_lane
from sellee.store import Store

HANDOFF = "you@example.com"
BUYER = "somebuyer@gmail.com"
RELAY = "a9f44b33155f3effb2904e25c5042f3d@reply.craigslist.org"
POSTING = "https://www.craigslist.org/view/d/san-francisco-dji-mic/q2Dcuy4bHvytRxM1T27fC9"
BODY = f"is it available?\n\n\n\nOriginal craigslist post:\n{POSTING}\nAbout craigslist mail:\nx"


class StubBus:
    def __init__(self):
        self.events = []

    def publish(self, name, payload=None):
        self.events.append((name, payload or {}))

    def names(self):
        return [name for name, _ in self.events]


class StubClient:
    """A browser that answers the mail artifacts from a script."""

    def __init__(self, *, listed=None, tail=None, account=None):
        self.navigated = []
        self.fresh_tabs = 0
        self.calls = []
        self._listed = listed
        self._tail = tail
        self._account = account or {"address": "you@example.com", "source": "account_label"}

    def exclusive(self):
        return _Null()

    def fresh_tab(self):
        self.fresh_tabs += 1
        return _Null()

    def navigate(self, url):
        self.navigated.append(url)

    def evaluate(self, function):
        if "could not read which account" in function:
            self.calls.append("account")
            return dict(self._account)
        if "no messages matched" in function:
            self.calls.append("list")
            return dict(self._listed or {"conversations": [], "empty_stated": True})
        if "no row for that conversation" in function:
            self.calls.append("open")
            return {"opened": "1a08", "rows": 1}
        if "no message bodies" in function:
            self.calls.append("tail")
            return dict(self._tail or {"error": "no message bodies in the opened conversation"})
        raise AssertionError("unscripted artifact")


class _Null:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


class Cfg:
    carousell_ai_web_base_url = "https://carousell.ai"


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as where:
        db = Database(pathlib.Path(where) / "t.db")
        for found in migrations.pending("data", db):
            migrations._apply(db, found)
        found = Store(db)
        found.set_seller_config_section("basics", {"region": "US", "currency": "USD"})
        yield found


def _deps(store, client, bus=None):
    return mail_lane.MailDeps(
        store=store,
        bus=bus or StubBus(),
        config=Cfg(),
        browser_factory=lambda: client,
        now=lambda: 1000.0,
    )


def _connected(store, bus):
    settings.set_now(store, bus, key="connected_markets", raw_value=["craigslist"])


def _signed_in(store):
    store.record_mail_probe("craigslist", provider="gmail", signed_in=True)


def _ready(store):
    _signed_in(store)
    store.record_mail_view("craigslist", "https://mail.google.com/mail/u/0/#search/x")
    store.record_mail_handoff("craigslist", HANDOFF, verified=True)


# --- a market nobody switched on ----------------------------------------------------------------


def test_a_market_the_seller_never_asked_for_is_left_alone(store) -> None:
    client = StubClient()
    mail_lane.mail_lane(_deps(store, client))
    assert client.fresh_tabs == 0
    assert store.mail_transport("craigslist") is None


# --- the migration path for an install that predates the transport ------------------------------


def test_a_switched_on_market_with_no_mailbox_is_asked_for_once(store) -> None:
    """The only route to a seller who was already using Craigslist before this existed: `sellee
    update` swaps the version tree and re-runs no setup phase, so nothing else would ever ask."""
    bus = StubBus()
    _connected(store, bus)
    client = StubClient()

    mail_lane.mail_lane(_deps(store, client, bus))
    assert [row["target"] for row in store.pending_connects()] == ["craigslist-mail"]
    asked = [text for text in _notices(store) if "second sign-in" in text or "mailbox" in text]
    assert asked, _notices(store)

    # Asked once, not once per tick: the lane runs on its own cadence and a notice per tick is a
    # market nagging its seller.
    before = len(_notices(store))
    mail_lane.mail_lane(_deps(store, client, bus))
    assert len(_notices(store)) == before


def _notices(store) -> list:
    return [row["text"] for row in store.claim_queued_notices(50)]


# --- finishing the connect ----------------------------------------------------------------------


def test_the_connect_is_finished_from_a_read_not_from_a_guess(store) -> None:
    """The view is recorded only after it has been read, and the handoff only after that. Written
    early, the seller flips from an actionable message to an unactionable one."""
    bus = StubBus()
    _connected(store, bus)
    _signed_in(store)
    client = StubClient(listed={"conversations": [], "empty_stated": True, "stale_rows": 0})

    mail_lane.mail_lane(_deps(store, client, bus))

    found = store.mail_transport("craigslist")
    assert found["handoff_address"] == HANDOFF
    assert "Original%20craigslist%20post" in found["view"]
    assert store.mail_ready("craigslist") is True
    assert "mail.connected" in bus.names()


def test_a_mailbox_with_no_buyer_mail_yet_still_connects(store) -> None:
    """An empty mailbox is the normal state at connect time. Read as a failure it would refuse
    every new seller."""
    bus = StubBus()
    _connected(store, bus)
    _signed_in(store)
    client = StubClient(listed={"conversations": [], "empty_stated": True, "stale_rows": 0})
    mail_lane.mail_lane(_deps(store, client, bus))
    assert store.mail_ready("craigslist") is True


def test_an_unreadable_view_records_nothing(store) -> None:
    bus = StubBus()
    _connected(store, bus)
    _signed_in(store)
    client = StubClient(listed={"error": "the mail view showed neither messages nor words"})
    mail_lane.mail_lane(_deps(store, client, bus))
    assert store.mail_ready("craigslist") is False
    assert not store.mail_transport("craigslist")["view"]


def test_an_unreadable_account_address_records_nothing(store) -> None:
    """The handoff is derived from the account's own address, and a `+tag` on the wrong domain
    would silently receive nothing."""
    bus = StubBus()
    _connected(store, bus)
    _signed_in(store)
    client = StubClient(account={"error": "could not read which account this mailbox belongs to"})
    mail_lane.mail_lane(_deps(store, client, bus))
    assert store.mail_ready("craigslist") is False


# --- reading ------------------------------------------------------------------------------------


def _a_buyer(store, bus):
    """A ready mailbox, one item live on craigslist, and a buyer message about it."""
    _connected(store, bus)
    _ready(store)
    item = store.create_item(title="DJI Mic Mini", list_price=30.0, currency="USD")
    store.record_listing_url(item["id"], "craigslist", POSTING)
    return item


def _client_with_a_message(sender=RELAY, name="sam"):
    return StubClient(
        listed={
            "conversations": [
                {
                    "provider_thread_id": "1a08",
                    "sender": sender,
                    "subject": "is it available?",
                    "unread": True,
                }
            ],
            "blocked": 0,
            "unidentified": 0,
            "duplicates": 0,
        },
        tail={
            "opened_thread_id": "1a08",
            "messages": [
                {
                    "provider_message_id": "m1",
                    "sender": sender,
                    "sender_name": name,
                    "recipients": [sender],
                    "body": BODY,
                }
            ],
            "blocked": 0,
            "unidentified": 0,
        },
    )


def test_a_buyer_message_becomes_a_thread_with_the_footer_stripped(store) -> None:
    bus = StubBus()
    item = _a_buyer(store, bus)
    client = _client_with_a_message()

    mail_lane.mail_lane(_deps(store, client, bus))

    thread = store.get_thread("craigslist:1a08")
    assert thread is not None
    assert thread["item_id"] == item["id"]
    assert thread["counterpart_handle"] == "sam"
    messages = store.get_thread_messages("craigslist:1a08", limit=None)
    assert [m["text"] for m in messages] == ["is it available?"]
    assert "craigslist.org" not in messages[0]["text"]


def test_the_read_happens_in_a_fresh_tab(store) -> None:
    bus = StubBus()
    _a_buyer(store, bus)
    client = _client_with_a_message()
    mail_lane.mail_lane(_deps(store, client, bus))
    assert client.fresh_tabs == 1


def test_every_inbound_message_carries_a_scam_verdict(store) -> None:
    """`tools/reply.py` gates a send on this being on the row, so a read path that skips it does
    not fail loudly — it silently opens the gate."""
    bus = StubBus()
    _a_buyer(store, bus)
    mail_lane.mail_lane(_deps(store, _client_with_a_message(), bus))
    messages = store.get_thread_messages("craigslist:1a08", limit=None)
    assert messages[0]["scam_verdict"] is not None


def test_a_message_folded_once_is_not_folded_again(store) -> None:
    """Without this the buyer appears to repeat themselves on every tick and the agent answers
    each repeat."""
    bus = StubBus()
    _a_buyer(store, bus)
    mail_lane.mail_lane(_deps(store, _client_with_a_message(), bus))
    mail_lane.mail_lane(_deps(store, _client_with_a_message(), bus))
    assert len(store.get_thread_messages("craigslist:1a08", limit=None)) == 1


def test_a_conversation_that_matches_no_item_is_said_out_loud(store) -> None:
    """`create_thread` refuses a sell thread with no item, so the buyer is unanswerable by
    construction — and for a mail market there is nowhere else the seller could see why."""
    bus = StubBus()
    _connected(store, bus)
    _ready(store)
    store.claim_queued_notices(50)
    mail_lane.mail_lane(_deps(store, _client_with_a_message(), bus))
    assert store.get_thread("craigslist:1a08") is None
    assert any("couldn't match" in text for text in _notices(store))
    assert "mail.unplaceable" in bus.names()


def test_a_forwarded_message_moves_the_conversation_off_the_relay(store) -> None:
    """The leg decides whether a payment link may ever be sent, and a *buyer* writing direct is
    what proves the address works."""
    bus = StubBus()
    _a_buyer(store, bus)
    mail_lane.mail_lane(_deps(store, _client_with_a_message(sender=BUYER, name="sam"), bus))
    assert store.mail_thread("1a08")["off_relay_ts"] is not None


def test_our_own_reply_does_not_move_the_conversation_off_the_relay(store) -> None:
    """The bug the content-scoped view introduced, caught before it shipped.

    The view now includes the agent's own replies — they quote craigslist's footer — and those come
    from the seller's own address. Counted as "not a relay sender", our first reply would move every
    conversation to the direct leg by itself, and the next payment link would go into the relay
    where it vanishes with no bounce: the seller believes the buyer was asked to pay and the buyer
    never saw anything.
    """
    bus = StubBus()
    _a_buyer(store, bus)
    mail_lane.mail_lane(_deps(store, _client_with_a_message(sender=HANDOFF, name="me"), bus))
    thread = store.mail_thread("1a08")
    assert thread is None or thread["off_relay_ts"] is None


def test_a_relay_message_leaves_the_conversation_on_the_relay(store) -> None:
    bus = StubBus()
    _a_buyer(store, bus)
    mail_lane.mail_lane(_deps(store, _client_with_a_message(), bus))
    assert store.mail_thread("1a08")["off_relay_ts"] is None


def test_the_newest_relay_address_is_what_a_reply_would_use(store) -> None:
    """Craigslist mints a fresh address per view — five were observed for one posting — so only
    the most recent has any chance of working."""
    bus = StubBus()
    _a_buyer(store, bus)
    mail_lane.mail_lane(_deps(store, _client_with_a_message(), bus))
    assert store.mail_thread("1a08")["relay_address"] == RELAY


def test_a_click_that_opened_the_wrong_conversation_folds_nothing(store) -> None:
    """Attributing a buyer's message to the wrong thread is the worst outcome on this path."""
    bus = StubBus()
    _a_buyer(store, bus)
    client = _client_with_a_message()
    client._tail = dict(client._tail, opened_thread_id="somethingelse")
    mail_lane.mail_lane(_deps(store, client, bus))
    assert store.get_thread("craigslist:1a08") is None


def test_an_unreadable_list_counts_toward_blindness(store) -> None:
    """A mailbox that has gone quiet for a reason we cannot see must escalate rather than fail
    silently forever."""
    bus = StubBus()
    _connected(store, bus)
    _ready(store)
    client = StubClient(listed={"error": "the mail view showed neither messages nor words"})
    deps = _deps(store, client, bus)
    mail_lane.mail_lane(deps)
    assert deps.blind["craigslist"] == 1
    assert "mail.blind" in bus.names()


def test_a_run_of_unreadable_reads_is_said_out_loud_once(store) -> None:
    """Silence is indistinguishable from "no buyers wrote", and on a market whose ads are live
    that is the difference between a quiet week and a broken transport."""
    bus = StubBus()
    _connected(store, bus)
    _ready(store)
    client = StubClient(listed={"error": "the mail view showed neither messages nor words"})
    deps = _deps(store, client, bus)
    store.claim_queued_notices(50)

    def blind_count() -> int:
        """How many blind notices are queued. Counted rather than drained, because claiming a
        notice does not consume it — delivery does."""
        return len([text for text in _notices(store) if "can't see them" in text])

    for _ in range(mail_lane.BLIND_AFTER - 1):
        mail_lane.mail_lane(deps)
    assert blind_count() == 0, "one bad read is ordinary"

    mail_lane.mail_lane(deps)
    assert blind_count() == 1

    # Once per run, not once per tick — every later failure is the same outage.
    for _ in range(3):
        mail_lane.mail_lane(deps)
    assert blind_count() == 1


def test_reading_again_is_said_only_to_a_seller_who_was_told_it_stopped(store) -> None:
    bus = StubBus()
    _connected(store, bus)
    _ready(store)
    broken = _deps(store, StubClient(listed={"error": "nothing came back"}), bus)
    for _ in range(mail_lane.BLIND_AFTER):
        mail_lane.mail_lane(broken)
    store.claim_queued_notices(50)

    working = mail_lane.MailDeps(
        store=store,
        bus=bus,
        config=Cfg(),
        browser_factory=lambda: StubClient(
            listed={"conversations": [], "empty_stated": True, "stale_rows": 0}
        ),
        now=lambda: 1000.0,
        blind=broken.blind,
    )
    mail_lane.mail_lane(working)
    assert any("buyers email again" in text for text in _notices(store))


# --- what the transport still delivers when it cannot answer ------------------------------------


def test_a_buyer_we_cannot_answer_is_passed_on_to_the_seller(store) -> None:
    """The value that survives the relay finding.

    Craigslist's relay carries a buyer's message to the seller and not a reply back, measured four
    times including a hand-typed control. So the transport reads the buyer, scam-scans them, joins
    them to the item — and tells the seller what they said, instead of the seller watching an
    inbox. The notice says why answering is theirs, because a seller told only "someone wrote"
    would wait for a reply we never sent.
    """
    bus = StubBus()
    _a_buyer(store, bus)
    store.claim_queued_notices(50)

    mail_lane.mail_lane(_deps(store, _client_with_a_message(), bus))

    said = [text for text in _notices(store) if "buyer wrote" in text]
    assert len(said) == 1, _notices(store)
    assert "is it available?" in said[0], "the buyer's own words"
    assert "won't carry my reply" in said[0], "why answering is theirs"
    assert "DJI Mic Mini" in said[0], "which listing"


def test_the_seller_is_told_once_per_message_not_once_per_tick(store) -> None:
    bus = StubBus()
    _a_buyer(store, bus)
    store.claim_queued_notices(50)
    for _ in range(3):
        mail_lane.mail_lane(_deps(store, _client_with_a_message(), bus))
    assert len([t for t in _notices(store) if "buyer wrote" in t]) == 1


def test_a_market_we_can_answer_is_not_double_reported(store) -> None:
    """Where the reply lane speaks, a notice here would be the seller hearing about it twice."""
    from sellee.browser import markets as market_adapters

    assert market_adapters.answers_buyers("carousell") is True
    assert market_adapters.answers_buyers("craigslist") is False
