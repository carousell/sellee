"""The registration lane and its reply sink: Craigslist mail read into sell threads and answered,
against a fake of bazaar's seller MCP server."""

from __future__ import annotations

import time
from email import message_from_bytes, policy
from email.utils import parseaddr
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.conftest import seed_setting
from tests.fake_carousell_ai_mcp import FakeRelay, serve

from sellee import daemon, marketplaces
from sellee.config import Config
from sellee.rail import registration
from sellee.rail.client import RailClient
from sellee.rail.registration_sink import RegistrationReplySink, reply_subject
from sellee.rail.sink import RelayReplySink
from sellee.tools.registry import dispatch

_WEB = "https://www.carousell.ai"
_FIXTURES = Path(__file__).parent / "fixtures" / "craigslist"
_POST = "https://www.craigslist.org/view/d/samsung-buds3/wXgUwq21B3QYeyVemvpPVE"
_OTHER_POST = "https://www.craigslist.org/view/d/teak-lamp/Zz9OtherPost00000000"
_BUYER = "47c1b3432c7f3097accc36bae6cdb3ef@reply.craigslist.org"
_THREAD = f"craigslist:{_BUYER}"
_FAST = Config(reply_delay_sec=(0, 0), interactive_reply_delay_sec=(0, 0))
_PROPERTY = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


@pytest.fixture
def fake():
    relay_state = FakeRelay()
    base, shutdown = serve(relay_state)
    relay_state.base = base
    yield relay_state
    shutdown()


@pytest.fixture
def item(store):
    """An item posted on Craigslist, with Craigslist connected."""
    seed_setting(store, "connected_markets", ["carousell", "fb", "craigslist"])
    made = store.create_item(title="Samsung Buds3", list_price=80.0, currency="USD")
    store.record_listing_url(made["id"], "craigslist", _POST)
    return store.get_item(made["id"])


def _client(fake, timeout_sec=5.0):
    return RailClient(api_base=fake.base, api_key="k", web_base_url=_WEB, timeout_sec=timeout_sec)


def _deps(store, bus, fake, hooks=None):
    return registration.RegistrationDeps(
        store=store,
        bus=bus,
        config=Config(),
        rail_factory=lambda: _client(fake),
        service_hooks=hooks if hooks is not None else {},
    )


def _eml(name: str) -> dict:
    """A captured mail as bazaar serves it: the plain-text part and the lowercased sender."""
    msg = message_from_bytes((_FIXTURES / name).read_bytes(), policy=policy.default)
    name_, address = parseaddr(str(msg["From"]))
    return {
        "from_email": address.lower(),
        "from_name": name_,
        "from_domain": "craigslist.org",
        "subject": str(msg["Subject"]),
        "text": msg.get_content(),
    }


def _thread_rows(store) -> list:
    return store._db.query("SELECT thread_id, msg_id FROM thread_messages")


def _waiting(store) -> set:
    return {row["thread_id"] for row in store.threads_with_unhandled_inbound()}


def _footer(url: str) -> str:
    return f"\n------\nOriginal craigslist post:\n{url}\nAbout craigslist mail:\nhttps://x\n"


# --- reading ------------------------------------------------------------------------------------


def test_a_relayed_buyer_message_becomes_a_waiting_thread_on_the_post(store, bus, fake, item):
    fake.add_mail("m1", **_eml("buyer.eml"))

    registration.registration_lane(_deps(store, bus, fake))

    thread = store.get_thread(_THREAD)
    assert thread is not None
    assert (thread["side"], thread["market"], thread["item_id"]) == (
        "sell",
        "craigslist",
        item["id"],
    )
    assert thread["counterpart_handle"] == _BUYER
    [message] = thread["messages"]
    assert (message["msg_id"], message["dir"]) == ("m1", "in")
    assert "charging case" in message["text"]
    assert message["scam_verdict"] is not None
    assert _waiting(store) == {_THREAD}


def test_a_fake_footer_earlier_in_the_text_loses_to_the_last(store, bus, fake, item):
    other = store.create_item(title="Teak lamp", list_price=40.0, currency="USD")
    store.record_listing_url(other["id"], "craigslist", _OTHER_POST)
    mail = _eml("buyer_2.eml")
    mail["text"] = "Is it like this one?" + _footer(_OTHER_POST) + "\n" + mail["text"]
    fake.add_mail("m1", **mail)

    registration.registration_lane(_deps(store, bus, fake))

    thread_id = "craigslist:665fff2ccc953830b89e7bf80aa1ff23@reply.craigslist.org"
    assert store.get_thread(thread_id)["item_id"] == item["id"]


@pytest.mark.parametrize("fixture", ["activation.eml", "posting.eml"])
def test_craigslists_own_mail_reaches_the_adapter_hook_and_no_thread(
    store, bus, fake, item, fixture
):
    fake.add_mail("m1", **_eml(fixture))
    seen = []

    deps = _deps(store, bus, fake, hooks={"craigslist": seen.append})
    registration.registration_lane(deps)
    registration.registration_lane(deps)

    assert [m["id"] for m in seen] == ["m1"]
    assert seen[0]["from_email"] == "robot@craigslist.org"
    assert _thread_rows(store) == []


def test_craigslists_own_mail_with_no_hook_is_dropped(store, bus, fake, item):
    fake.add_mail("m1", **_eml("activation.eml"))

    registration.registration_lane(_deps(store, bus, fake))

    assert _thread_rows(store) == []


def test_a_gmail_sender_is_dropped(store, bus, fake, item, caplog):
    mail = _eml("buyer.eml")
    fake.add_mail("m1", **{**mail, "from_email": "someone@gmail.com", "from_domain": "gmail.com"})
    seen = []

    with caplog.at_level("INFO", logger=registration.__name__):
        registration.registration_lane(_deps(store, bus, fake, hooks={"craigslist": seen.append}))

    assert _thread_rows(store) == []
    assert seen == []
    assert "registration mail m1 from 'gmail.com' is no marketplace's" in caplog.text


def test_a_buyer_whose_post_is_no_item_of_ours_reaches_no_hook(store, bus, fake, item):
    mail = _eml("buyer.eml")
    fake.add_mail("m1", **{**mail, "text": "Still there?" + _footer(_OTHER_POST)})
    seen = []

    registration.registration_lane(_deps(store, bus, fake, hooks={"craigslist": seen.append}))

    assert _thread_rows(store) == []
    assert seen == []


def test_a_second_message_from_the_buyer_joins_the_same_thread(store, bus, fake, item):
    deps = _deps(store, bus, fake)
    fake.add_mail("m1", **_eml("buyer.eml"))
    registration.registration_lane(deps)
    mail = _eml("buyer.eml")
    fake.add_mail(
        "m2", **{**mail, "subject": "Re: Samsung Buds3", "text": "Hello?" + _footer(_POST)}
    )

    registration.registration_lane(deps)

    assert [m["msg_id"] for m in store.get_thread(_THREAD)["messages"]] == ["m1", "m2"]


def test_an_out_of_office_is_stored_on_its_thread_but_never_answered(store, bus, fake, item):
    mail = _eml("buyer.eml")
    fake.add_mail(
        "m1", **{**mail, "text": "I am away until Monday." + _footer(_POST)}, automatic=True
    )

    registration.registration_lane(_deps(store, bus, fake))

    assert [m["msg_id"] for m in store.get_thread(_THREAD)["messages"]] == ["m1"]
    assert _waiting(store) == set()


def test_an_out_of_office_after_an_unanswered_question_leaves_it_waiting(store, bus, fake, item):
    mail = _eml("buyer.eml")
    fake.add_mail("m1", **mail)
    fake.add_mail("m2", **{**mail, "text": "Away until Monday." + _footer(_POST)}, automatic=True)

    registration.registration_lane(_deps(store, bus, fake))

    assert _waiting(store) == {_THREAD}


def test_the_cursor_pages_through_more_mail_than_one_page(store, bus, fake, item, monkeypatch):
    monkeypatch.setattr(registration, "PAGE_SIZE", 2)
    mail = _eml("buyer.eml")
    for n in range(5):
        fake.add_mail(f"m{n}", **{**mail, "from_email": f"b{n}@reply.craigslist.org"})

    registration.registration_lane(_deps(store, bus, fake))

    assert _waiting(store) == {f"craigslist:b{n}@reply.craigslist.org" for n in range(5)}
    assert store.get_registration_cursor() != ""


def test_the_rail_down_for_a_tick_raises_nothing_and_the_next_catches_up(store, bus, fake, item):
    fake.add_mail("m1", **_eml("buyer.eml"))
    deps = _deps(store, bus, fake)
    fake.down = True

    registration.registration_lane(deps)

    assert store.get_thread(_THREAD) is None
    fake.down = False
    registration.registration_lane(deps)
    assert _waiting(store) == {_THREAD}


def test_an_unprovisioned_rail_is_skipped_quietly(store, bus, item):
    def unprovisioned():
        from sellee.rail.client import RailUnprovisioned

        raise RailUnprovisioned("carousell.ai is not provisioned")

    registration.registration_lane(
        registration.RegistrationDeps(
            store=store, bus=bus, config=Config(), rail_factory=unprovisioned
        )
    )

    assert store.get_registration_cursor() == ""


def test_the_lookup_resolves_a_registered_domain_to_its_marketplace():
    assert marketplaces.market_for_domain("craigslist.org") == "craigslist"
    assert marketplaces.market_for_domain("carousell.com.my") == "carousell"
    assert marketplaces.market_for_domain("gmail.com") is None
    assert marketplaces.market_for_domain("org") is None
    assert marketplaces.market_for_domain("") is None


# --- replying -----------------------------------------------------------------------------------


def _factory(store, bus, fake):
    """The daemon's reply sink factory, with a browser that must never be asked for."""

    def no_browser():
        raise AssertionError("a Craigslist reply must not acquire the browser")

    def factory(market):
        sink = daemon.reply_sink_for(
            market,
            store=store,
            bus=bus,
            cfg=_FAST,
            rail_factory=lambda: _client(fake),
            browser_factory=no_browser,
        )
        if isinstance(sink, RegistrationReplySink):
            sink._retry_delays_sec = (0, 0, 0)
        return sink

    return factory


def _send(make_ctx, store, bus, fake, thread_id=_THREAD, text="Yes, still available!"):
    ctx = make_ctx("attended", config=_FAST)
    ctx.reply_sink = _factory(store, bus, fake)
    return dispatch("send_reply", {"thread_id": thread_id, "text": text}, ctx)


@pytest.fixture
def waiting(store, bus, fake, item):
    fake.add_mail("m1", **_eml("buyer.eml"))
    registration.registration_lane(_deps(store, bus, fake))
    return fake


def _intent_statuses(store) -> list:
    return [row["status"] for row in store._db.query("SELECT status FROM send_intents")]


def test_a_reply_reaches_send_registration_reply_under_the_intent_id(make_ctx, store, bus, waiting):
    res = _send(make_ctx, store, bus, waiting)

    assert res["status"] == "sent"
    [sent] = waiting.registration_sent
    assert sent["to"] == _BUYER
    assert sent["client_message_id"] == res["intent_id"]
    assert sent["subject"] == "Re: Samsung Buds3"
    assert sent["text"] == "Yes, still available!"
    assert _waiting(store) == set()
    assert _intent_statuses(store) == ["committed"]


def test_a_craigslist_thread_is_not_answered_while_craigslist_is_not_connected(
    make_ctx, store, bus, waiting
):
    seed_setting(store, "connected_markets", ["carousell", "fb"])

    res = _send(make_ctx, store, bus, waiting)

    assert res["status"] == "not_connected"
    assert waiting.registration_calls == []


def test_a_carousell_ai_thread_still_gets_the_relay_sink(store, bus, fake):
    assert isinstance(_factory(store, bus, fake)("carousell-ai"), RelayReplySink)
    assert not isinstance(_factory(store, bus, fake)("carousell-ai"), RegistrationReplySink)


@pytest.mark.parametrize("failure", ["unavailable", "http503", "lost"])
def test_a_failed_send_is_retried_under_the_same_id(make_ctx, store, bus, waiting, failure):
    waiting.registration_script = [failure]

    res = _send(make_ctx, store, bus, waiting)

    assert res["status"] == "sent"
    assert {c["client_message_id"] for c in waiting.registration_calls} == {res["intent_id"]}
    assert len(waiting.registration_calls) == 2
    assert len(waiting.registration_sent) == 1


def test_a_refusal_fails_the_send_without_a_retry(make_ctx, store, bus, waiting):
    waiting.correspondents.clear()

    res = _send(make_ctx, store, bus, waiting)

    assert res["status"] == "send_failed" and res["delivered"] == "no"
    assert len(waiting.registration_calls) == 1
    assert _intent_statuses(store) == []


def _stuck(make_ctx, store, bus, fake):
    fake.registration_script = ["unavailable"] * 4
    res = _send(make_ctx, store, bus, fake)
    assert res["status"] == "send_unverified" and fake.registration_sent == []
    return res["intent_id"]


def _later(store, bus, fake, after_sec):
    deps = _deps(store, bus, fake)
    deps.now = lambda: time.time() + after_sec
    registration.registration_lane(deps)


def test_the_lane_finishes_a_send_no_attempt_got_through(make_ctx, store, bus, waiting):
    intent = _stuck(make_ctx, store, bus, waiting)

    _later(store, bus, waiting, 1)
    assert waiting.registration_sent == []

    _later(store, bus, waiting, registration.RETRY_SEND_AFTER_SEC + 1)

    [sent] = waiting.registration_sent
    assert sent["client_message_id"] == intent and sent["to"] == _BUYER
    assert _intent_statuses(store) == ["committed"]
    assert _waiting(store) == set()


def test_a_retry_bazaar_refuses_drops_the_send(make_ctx, store, bus, waiting):
    _stuck(make_ctx, store, bus, waiting)
    waiting.correspondents.clear()

    _later(store, bus, waiting, registration.RETRY_SEND_AFTER_SEC + 1)

    assert _intent_statuses(store) == []


def test_a_paused_agent_retries_nothing_from_the_lane(make_ctx, store, bus, waiting):
    _stuck(make_ctx, store, bus, waiting)
    calls = len(waiting.registration_calls)
    store.set_paused(True)

    _later(store, bus, waiting, registration.RETRY_SEND_AFTER_SEC + 1)

    assert len(waiting.registration_calls) == calls


def test_the_sweep_never_asks_the_seller_about_a_craigslist_send(make_ctx, store, bus, waiting):
    _stuck(make_ctx, store, bus, waiting)

    assert store.stale_intent_sweep(grace_sec=600, now=time.time() + 30 * 86400) == []


def test_a_craigslist_reply_ignores_quiet_hours_and_the_pacing_cap(make_ctx, store, bus, waiting):
    from sellee.browser import inbox as browser_inbox

    now = time.localtime()
    hour = now.tm_hour
    seed_setting(store, "quiet_hours", [((hour - 1) % 24) * 100, ((hour + 2) % 24) * 100])

    assert "craigslist" not in browser_inbox.paced_out_markets(store, Config())
    res = _send(make_ctx, store, bus, waiting)

    assert res["status"] == "sent"
    assert store._db.query("SELECT 1 FROM pacing_actions WHERE marketplace = 'craigslist'") == []


def test_a_sale_on_a_craigslist_thread_records_craigslist(store, bus, waiting, item):
    store.record_listing_url(item["id"], "carousell-ai", f"{_WEB}/listing/L1")

    sold = store.negotiate_confirm_sold(item["id"], _THREAD)

    assert {t["platform"] for t in sold["take_down"]} == {"carousell-ai"}


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Samsung Buds3", "Re: Samsung Buds3"),
        ("", "Re: your message"),
        ("  ", "Re: your message"),
        ("Two\r\nlines", "Re: Two lines"),
        ("Re: Samsung Buds3", "Re: Samsung Buds3"),
    ],
)
def test_the_reply_subject_is_one_line(subject, expected):
    assert reply_subject(subject) == expected


# --- properties ---------------------------------------------------------------------------------

_texts = st.text(st.characters(blacklist_categories=("Cs",)), max_size=200)
_tokens = st.text("0123456789abcdef", min_size=6, max_size=32)


@_PROPERTY
@given(st.lists(st.sampled_from(["tick", "repeat", "crash", "rewind"]), min_size=1, max_size=8))
def test_property_one_mail_appends_one_message_however_often_it_is_read(
    store, bus, fake, item, monkeypatch, schedule
):
    """Property: reading the same mail on any number of ticks, refetches and crashes appends one
    thread message for it."""
    deps = _deps(store, bus, fake)
    mail_id = f"m{len(fake.mail)}"
    fake.add_mail(mail_id, **{**_eml("buyer.eml"), "text": f"{mail_id}?" + _footer(_POST)})
    real = store.set_registration_cursor
    for step in schedule:
        if step == "rewind":
            real("")
        fake.repeat_tail = step == "repeat"
        if step == "crash":
            monkeypatch.setattr(store, "set_registration_cursor", _raise)
        try:
            registration.registration_lane(deps)
        except RuntimeError:
            pass
        monkeypatch.setattr(store, "set_registration_cursor", real)
    registration.registration_lane(deps)

    ids = [m["msg_id"] for m in store.get_thread(_THREAD)["messages"]]
    assert ids == [m["id"] for m in fake.mail]


@_PROPERTY
@given(st.lists(st.lists(st.booleans(), min_size=1, max_size=4), min_size=1, max_size=4))
def test_property_only_a_mail_from_a_person_makes_a_thread_wait(
    make_ctx, store, bus, fake, item, ticks
):
    """Property: after any run of buyer mail read over any ticks, the thread waits exactly when a
    mail not marked automatic has arrived since the last reply."""
    deps = _deps(store, bus, fake)
    buyer = f"b{len(fake.mail)}@reply.craigslist.org"
    thread_id = f"craigslist:{buyer}"
    owed = False
    for batch in ticks:
        for automatic in batch:
            mail_id = f"m{len(fake.mail)}"
            fake.add_mail(
                mail_id,
                from_email=buyer,
                from_domain="craigslist.org",
                subject="Samsung Buds3",
                text=mail_id + _footer(_POST),
                automatic=automatic,
            )
            owed = owed or not automatic
        registration.registration_lane(deps)
        assert (thread_id in _waiting(store)) == owed
        if owed:
            assert _send(make_ctx, store, bus, fake, thread_id=thread_id)["status"] == "sent"
            owed = False


def _raise(_cursor):
    raise RuntimeError("killed before the cursor was stored")


_other_domains = st.one_of(
    st.sampled_from(
        [
            "gmail.com",
            "icloud.com",
            "reply.craigslist.org",
            "craigslist.org.evil.com",
            "notcraigslist.org",
            "craigslist.com",
            "carousell.ai",
            "org",
            "",
        ]
    ),
    st.from_regex(r"\A[a-z0-9-]{1,12}(\.[a-z]{2,6}){1,2}\Z"),
).filter(lambda d: d != "craigslist.org")


@_PROPERTY
@given(_other_domains, _tokens, _texts)
def test_property_mail_not_from_craigslist_org_never_becomes_a_message(
    store, bus, fake, item, domain, token, text
):
    """Property: whatever its sender address and text, mail whose from_domain is not
    craigslist.org never becomes a thread message."""
    fake.add_mail(
        f"m{len(fake.mail)}",
        from_email=f"{token}@reply.craigslist.org",
        from_domain=domain,
        subject="Samsung Buds3",
        text=text + _footer(_POST),
    )

    registration.registration_lane(_deps(store, bus, fake, hooks={"craigslist": lambda m: None}))

    assert _thread_rows(store) == []


_not_our_post = st.one_of(
    st.just(""),
    st.just(_POST + "x"),
    st.just(_POST.upper()),
    st.just(_POST.replace("https", "http")),
    st.just(_OTHER_POST),
    st.from_regex(r"\Ahttps://[a-z]{1,8}\.craigslist\.org/[a-z/]{0,20}\Z"),
)


@_PROPERTY
@given(_tokens, _texts, st.lists(st.sampled_from([_POST, _OTHER_POST]), max_size=3), _not_our_post)
def test_property_a_last_footer_naming_no_item_makes_no_thread(
    store, bus, fake, item, token, text, earlier, last
):
    """Property: a Craigslist buyer's mail whose last footer names no item's post makes no thread,
    whatever footers come before it."""
    body = text + "".join(_footer(url) for url in earlier) + _footer(last)
    fake.add_mail(
        f"m{len(fake.mail)}",
        from_email=f"{token}@reply.craigslist.org",
        from_domain="craigslist.org",
        subject="Samsung Buds3",
        text=body,
    )

    registration.registration_lane(_deps(store, bus, fake))

    assert store._db.query("SELECT thread_id FROM threads") == []


@_PROPERTY
@given(_tokens, st.text(st.characters(blacklist_categories=("Cs", "Cc")), min_size=1, max_size=80))
def test_property_a_craigslist_thread_is_always_answered_by_the_registration_sink(
    make_ctx, store, bus, fake, item, token, subject
):
    """Property: every Craigslist thread's reply leaves through send_registration_reply, to its
    buyer, under its intent's id, with a one-line subject."""
    buyer = f"{token}@reply.craigslist.org"
    fake.add_mail(
        f"m{len(fake.mail)}",
        from_email=buyer,
        from_domain="craigslist.org",
        subject=subject,
        text="Still available?" + _footer(_POST),
    )
    registration.registration_lane(_deps(store, bus, fake))
    thread_id = f"craigslist:{buyer}"
    if thread_id not in _waiting(store):
        return
    assert isinstance(_factory(store, bus, fake)("craigslist"), RegistrationReplySink)

    res = _send(make_ctx, store, bus, fake, thread_id=thread_id)

    assert res["status"] == "sent"
    sent = fake.registration_sent[-1]
    assert (sent["to"], sent["client_message_id"]) == (buyer, res["intent_id"])
    assert "\n" not in sent["subject"] and sent["subject"].startswith("Re: ")
