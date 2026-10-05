"""A signed-out Craigslist account signs itself back in: sellee asks for a login link, the
registration lane records it, and the account lane opens it once. Nobody signs in by hand."""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.conftest import seed_setting
from tests.fake_carousell_ai_mcp import FakeRelay, serve
from tests.test_craigslist_account import (
    _ADDRESS,
    _PROPERTY,
    Clock,
    FakeCraigslist,
    _activation_mail,
    _deps,
    _reset,
    _state,
)

from sellee.browser import craigslist_account as account
from sellee.browser.client import BrowserToolError
from sellee.browser.markets import craigslist

_LOGIN_LINK = "https://accounts.craigslist.org/login/onetime?key=8a7c2f-91ab&userid=410294913"
_SAFE = re.compile(r"^https://accounts\.craigslist\.org/(pass|login/onetime)\?\S+$")


@pytest.fixture
def fake():
    relay_state = FakeRelay()
    relay_state.registration_email = _ADDRESS
    base, shutdown = serve(relay_state)
    relay_state.base = base
    yield relay_state
    shutdown()


@pytest.fixture
def us_seller(store):
    store.set_seller_config_section("basics", {"region": "US"})
    seed_setting(store, "connected_markets", ["craigslist"])
    return store


class FakeLogin(FakeCraigslist):
    """The account pages plus the login form's "E-mail a login link" and the link it sends."""

    def __init__(self, *, link_signs_in=True, terms=False, sends=True, **kwargs):
        super().__init__(terms=terms, **kwargs)
        self.link_signs_in = link_signs_in
        self.sends = sends
        self.link_requests = 0

    def navigate(self, url: str) -> None:
        if url.startswith("https://accounts.craigslist.org/login/onetime?"):
            self.opened.append(url)
            self.pending = None
            if not self.link_signs_in:
                self._arrive("unknown")
            else:
                self._arrive("terms" if self.terms else "account_home")
            return
        super().navigate(url)

    def click(self, target: str, element: str) -> None:
        if target == craigslist.LOGIN_LINK_BUTTON and self.kind == "login":
            self.clicked.append(target)
            self.link_requests += 1
            self._arrive("login_link_sent" if self.sends else "unknown")
            return
        super().click(target, element)


def _active(store) -> None:
    store.request_craigslist_signup()
    store.set_craigslist_awaiting_activation()
    store.activate_craigslist_account("ready")
    for notice in store.claim_queued_notices(10):
        store.mark_notice_delivered(notice["id"], "channel")


def _login_mail(text: str | None = None) -> dict:
    mail = _activation_mail()
    return {
        **mail,
        "id": "m-login",
        "subject": "craigslist login link",
        "text": f"log in to craigslist\n\n{_LOGIN_LINK}\n" if text is None else text,
    }


def _notices(store) -> list:
    claimed = store.claim_queued_notices(10)
    for notice in claimed:
        store.mark_notice_delivered(notice["id"], "channel")
    return [n["text"] for n in claimed]


@pytest.fixture
def signed_out(us_seller):
    _active(us_seller)
    assert account.signed_out(us_seller, "craigslist")
    return us_seller


# --- the hook ------------------------------------------------------------------------------------

_TEXTS = st.one_of(
    st.text(max_size=200),
    st.builds(
        "{} {}://{}/{}?{} {}".format,
        st.text(max_size=30),
        st.sampled_from(["https", "http"]),
        st.sampled_from(
            ["accounts.craigslist.org", "accounts.craigslist.org.evil.example", "evil.example"]
        ),
        st.sampled_from(["login/onetime", "pass", "login/onetimex", "login"]),
        st.text(alphabet="key=abc123&-", max_size=20),
        st.text(max_size=30),
    ),
)


@_PROPERTY
@given(text=_TEXTS, waiting=st.sampled_from(["signup_requested", "awaiting_login_link"]))
def test_only_craigslists_own_account_links_are_ever_recorded(store, text, waiting) -> None:
    _reset(store)
    _active(store)
    if waiting == "awaiting_login_link":
        store.request_craigslist_login()
    else:
        _reset(store)
        store.request_craigslist_signup()

    account.record_service_mail(store, _login_mail(text))

    link = store.craigslist_account()["link"]
    assert link is None or _SAFE.match(link)


def test_a_login_link_is_recorded_only_while_one_is_awaited(store) -> None:
    _active(store)
    account.record_service_mail(store, _login_mail())
    assert store.craigslist_account()["link"] is None

    store.request_craigslist_login()
    account.record_service_mail(store, _login_mail())
    assert store.craigslist_account()["link"] == _LOGIN_LINK


# --- the lane ------------------------------------------------------------------------------------


def test_a_signed_out_account_asks_for_a_login_link_and_signs_back_in(signed_out, bus, fake):
    page = FakeLogin(terms=True)
    deps = _deps(signed_out, bus, page, fake)

    account.account_lane(deps)

    assert page.link_requests == 1
    assert (craigslist.LOGIN_EMAIL, _ADDRESS) in page.typed
    assert _state(signed_out) == account.AWAITING_LOGIN_LINK

    account.record_service_mail(signed_out, _login_mail())
    account.account_lane(deps)

    assert page.opened[-1] == _LOGIN_LINK
    assert craigslist.ACCEPT_TERMS in page.clicked
    assert _state(signed_out) == account.ACTIVE
    assert _notices(signed_out) == []


@_PROPERTY
@given(gaps=st.lists(st.floats(0, 2400), min_size=1, max_size=12))
def test_at_most_one_login_link_request_is_in_flight(store, bus, fake, gaps) -> None:
    _reset(store)
    _active(store)
    store.request_craigslist_login()
    clock = Clock()
    page = FakeLogin()
    deps = _deps(store, bus, page, fake, clock=clock)
    sent_at: list = []

    for gap in gaps:
        before = page.link_requests
        account.account_lane(deps)
        if page.link_requests > before:
            sent_at.append(clock.t)
        clock.t += gap

    assert sent_at, "the first tick asks"
    assert all(b - a >= account.LOGIN_WAIT_SEC for a, b in zip(sent_at, sent_at[1:]))


def test_no_login_link_within_the_wait_is_reported_once_and_asked_again(signed_out, bus, fake):
    clock = Clock()
    page = FakeLogin()
    deps = _deps(signed_out, bus, page, fake, clock=clock)

    account.account_lane(deps)
    for _ in range(3):
        clock.t += account.LOGIN_WAIT_SEC + 1
        account.account_lane(deps)

    assert page.link_requests == 4
    assert _notices(signed_out) == [account.LOGIN_LATE_NOTICE]


def test_a_login_mail_read_twice_opens_its_link_once(signed_out, bus, fake) -> None:
    page = FakeLogin()
    deps = _deps(signed_out, bus, page, fake)
    account.account_lane(deps)

    account.record_service_mail(signed_out, _login_mail())
    account.account_lane(deps)
    account.record_service_mail(signed_out, _login_mail())
    account.account_lane(deps)

    assert page.opened.count(_LOGIN_LINK) == 1


def test_a_link_that_does_not_sign_in_asks_for_a_new_one_and_keeps_the_account(
    signed_out, bus, fake
) -> None:
    page = FakeLogin(link_signs_in=False)
    deps = _deps(signed_out, bus, page, fake)
    account.account_lane(deps)
    account.record_service_mail(signed_out, _login_mail())

    account.account_lane(deps)  # opens the link; still signed out
    account.account_lane(deps)  # settles: the link is spent
    account.account_lane(deps)  # asks again

    assert _state(signed_out) == account.AWAITING_LOGIN_LINK
    assert page.link_requests == 2


def test_an_account_page_already_signed_in_needs_no_link(signed_out, bus, fake) -> None:
    page = FakeLogin()
    page.signed_in = True
    page.navigate = lambda url: (page.opened.append(url), page._arrive("account_home"))

    account.account_lane(_deps(signed_out, bus, page, fake))

    assert page.link_requests == 0
    assert _state(signed_out) == account.ACTIVE


def test_a_browser_failure_leaves_the_request_to_the_next_tick(signed_out, bus, fake) -> None:
    page = FakeLogin()

    def refuse(url):
        raise BrowserToolError("Chrome went away")

    page.navigate = refuse
    account.account_lane(_deps(signed_out, bus, page, fake))

    assert signed_out.craigslist_account()["requested_ts"] == 0


def test_a_failed_login_never_drops_the_account(signed_out) -> None:
    assert not signed_out.abandon_craigslist_account("gone")
    assert _state(signed_out) == account.AWAITING_LOGIN_LINK


# --- connecting ----------------------------------------------------------------------------------


def test_connecting_a_signed_out_account_answers_signing_back_in(us_seller) -> None:
    _active(us_seller)

    assert account.connect_state(us_seller, "craigslist", "logged_out") == account.SIGNING_BACK_IN
    assert _state(us_seller) == account.AWAITING_LOGIN_LINK
    assert account.connect_state(us_seller, "craigslist", "logged_out") == account.SIGNING_BACK_IN


def test_connecting_with_no_account_still_signs_up(us_seller) -> None:
    assert account.connect_state(us_seller, "craigslist", "logged_out") == account.SIGNING_UP
    assert _state(us_seller) == account.SIGNUP_REQUESTED
