"""The seller's Craigslist account: sellee signs up with the registration address, the activation
mail's link is recorded by the registration lane, and the account lane opens it."""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from email import message_from_bytes, policy
from email.utils import parseaddr
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from tests.conftest import seed_setting
from tests.fake_carousell_ai_mcp import FakeRelay, serve

from sellee.browser import craigslist_account as account
from sellee.browser.client import BrowserToolError
from sellee.browser.markets import craigslist
from sellee.config import Config
from sellee.rail import registration
from sellee.rail.client import RailClient

_FIXTURES = Path(__file__).parent / "fixtures" / "craigslist"
_ADDRESS = "7adtxcmldqu4zyi6vgkhq6nrrq@inbox.carousell.ai"
_LINK = (
    "https://accounts.craigslist.org/pass?key=331764570-83nFXrgvkh7DDTEd2vZgcQxa&userid=410294913"
)
_SAFE_LINK = re.compile(r"^https://accounts\.craigslist\.org/pass\?\S+$")
_PROPERTY = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


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


def _rail(fake):
    return RailClient(api_base=fake.base, api_key="k", web_base_url="https://www.carousell.ai")


def _activation_mail(text: str | None = None) -> dict:
    msg = message_from_bytes((_FIXTURES / "activation.eml").read_bytes(), policy=policy.default)
    _name, address = parseaddr(str(msg["From"]))
    body = msg.get_content().replace(
        "https://accounts.craigslist.org/pass?key=REDACTED&userid=REDACTED", _LINK
    )
    return {
        "id": "m-activation",
        "from_email": address.lower(),
        "from_domain": "craigslist.org",
        "subject": str(msg["Subject"]),
        "text": body if text is None else text,
        "automatic": False,
        "received_at": "2026-10-05T08:00:00Z",
    }


class FakeCraigslist:
    """Craigslist's account pages as the lane sees them: what each click leads to."""

    def __init__(self, *, terms: bool = False, sign_up_page: str = "signup_sent"):
        self.kind = "blank"
        self.terms = terms
        self.sign_up_page = sign_up_page
        self.opened: list = []
        self.typed: list = []
        self.clicked: list = []

    @contextmanager
    def exclusive(self):
        yield

    def navigate(self, url: str) -> None:
        self.opened.append(url)
        if url.startswith("https://accounts.craigslist.org/pass?"):
            self.kind = "password_options"
        elif url == craigslist.LOGIN_URL:
            self.kind = "login"
        else:
            self.kind = "account_home" if self.kind in ("account_home",) else "unknown"

    def evaluate(self, function: str, **_):
        if function == craigslist.PAGE_JS:
            return {"kind": self.kind}
        if function == craigslist.LOGIN_JS:
            return {"state": "logged_in" if self.kind == "account_home" else "logged_out"}
        raise AssertionError("unexpected script")

    def type_humanly(self, target: str, element: str, text: str) -> None:
        self.typed.append((target, text))

    def click(self, target: str, element: str) -> None:
        self.clicked.append(target)
        if target == craigslist.SIGN_UP_BUTTON and self.kind == "login":
            self.kind = self.sign_up_page
        elif target == craigslist.GO_PASSWORDLESS and self.kind == "password_options":
            self.kind = "terms" if self.terms else "account_home"
        elif target == craigslist.ACCEPT_TERMS and self.kind == "terms":
            self.kind = "account_home"
        else:
            raise BrowserToolError(f"nothing to click at {target} on {self.kind}")


def _deps(store, bus, page, fake, now=time.time):
    return account.AccountDeps(
        store=store,
        bus=bus,
        browser_factory=lambda: page,
        rail_factory=lambda: _rail(fake),
        now=now,
    )


def _reset(store) -> None:
    with store._db.transaction() as conn:
        conn.execute("DELETE FROM craigslist_account")


def _state(store) -> str | None:
    row = store.craigslist_account()
    return row["state"] if row else None


# --- the hook: recording the activation link ---------------------------------------------------

_URLS = st.one_of(
    st.just(_LINK),
    st.builds(
        "{}://{}{}{}".format,
        st.sampled_from(["https", "http"]),
        st.sampled_from(
            [
                "accounts.craigslist.org",
                "accounts.craigslist.org.evil.example",
                "evil.example",
                "craigslist.org",
                "post.craigslist.org",
            ]
        ),
        st.sampled_from(["/pass?", "/pass", "/login/onetime?", "/", "/pass?x=1/../"]),
        st.text(alphabet="abcdefXYZ0123456789=&-_%./:@", max_size=30),
    ),
)
_BODIES = st.lists(st.one_of(_URLS, st.text(max_size=40)), max_size=6).map(" \n".join)


@_PROPERTY
@given(text=_BODIES)
@example(text="https://accounts.craigslist.org.evil.example/pass?key=1")
@example(text="https://evil.example/?next=accounts.craigslist.org/pass?key=1")
def test_only_an_accounts_craigslist_pass_link_is_ever_recorded(store, text) -> None:
    _reset(store)
    store.request_craigslist_signup()
    store.set_craigslist_awaiting_activation()

    account.record_service_mail(store, _activation_mail(text))

    link = store.craigslist_account()["link"]
    assert link is None or _SAFE_LINK.match(link)


def test_an_activation_mail_records_its_link_while_awaiting_activation(store) -> None:
    store.request_craigslist_signup()
    store.set_craigslist_awaiting_activation()

    account.record_service_mail(store, _activation_mail())

    assert store.craigslist_account()["link"] == _LINK


def test_an_activation_mail_with_no_sign_up_of_ours_is_ignored(store) -> None:
    account.record_service_mail(store, _activation_mail())

    assert store.craigslist_account() is None


def test_an_activation_mail_for_an_active_account_changes_nothing(store) -> None:
    store.request_craigslist_signup()
    store.set_craigslist_awaiting_activation()
    store.activate_craigslist_account("ready")

    account.record_service_mail(store, _activation_mail())

    row = store.craigslist_account()
    assert (row["state"], row["link"]) == (account.ACTIVE, None)


def test_the_registration_lane_hands_craigslists_own_mail_to_the_account(store, bus, fake) -> None:
    store.request_craigslist_signup()
    store.set_craigslist_awaiting_activation()
    mail = _activation_mail()
    fake.add_mail("m1", **{k: v for k, v in mail.items() if k not in ("id", "received_at")})

    registration.registration_lane(
        registration.RegistrationDeps(
            store=store,
            bus=bus,
            config=Config(),
            rail_factory=lambda: _rail(fake),
            service_hooks=account.service_hooks(store),
        )
    )

    assert store.craigslist_account()["link"] == _LINK


# --- the account's states ------------------------------------------------------------------------

_EDGES = {
    (None, account.SIGNUP_REQUESTED),
    (account.SIGNUP_REQUESTED, account.AWAITING_ACTIVATION),
    (account.AWAITING_ACTIVATION, account.ACTIVE),
}
_STEPS = st.lists(
    st.sampled_from(["request", "signed_up", "link", "activate"]), min_size=1, max_size=12
)


@_PROPERTY
@given(steps=_STEPS)
def test_the_account_only_moves_forward_through_its_states(store, steps) -> None:
    _reset(store)
    for step in steps:
        before = _state(store)
        if step == "request":
            store.request_craigslist_signup()
        elif step == "signed_up":
            store.set_craigslist_awaiting_activation()
        elif step == "link":
            account.record_service_mail(store, _activation_mail())
        else:
            store.activate_craigslist_account("ready")
        after = _state(store)
        assert before == after or (before, after) in _EDGES


# --- the lane: signing up and activating -------------------------------------------------------


def test_a_requested_sign_up_submits_the_registration_address(us_seller, bus, fake) -> None:
    page = FakeCraigslist()
    us_seller.request_craigslist_signup()

    account.account_lane(_deps(us_seller, bus, page, fake))

    assert page.opened == [craigslist.LOGIN_URL]
    assert page.typed == [(craigslist.SIGN_UP_EMAIL, _ADDRESS)]
    assert page.clicked == [craigslist.SIGN_UP_BUTTON]
    assert _state(us_seller) == account.AWAITING_ACTIVATION


def test_a_sign_up_craigslist_does_not_confirm_tells_the_seller_and_can_be_asked_again(
    us_seller, bus, fake
) -> None:
    page = FakeCraigslist(sign_up_page="unknown")
    us_seller.request_craigslist_signup()

    account.account_lane(_deps(us_seller, bus, page, fake))

    assert us_seller.craigslist_account() is None
    assert [n["text"] for n in us_seller.list_queued_notices()] == [account.SIGN_UP_FAILED_NOTICE]


def test_a_recorded_link_is_opened_once_and_the_account_goes_passwordless(
    us_seller, bus, fake
) -> None:
    page = FakeCraigslist()
    us_seller.request_craigslist_signup()
    us_seller.set_craigslist_awaiting_activation()
    account.record_service_mail(us_seller, _activation_mail())
    account.record_service_mail(us_seller, _activation_mail())

    account.account_lane(_deps(us_seller, bus, page, fake))
    account.account_lane(_deps(us_seller, bus, page, fake))

    assert page.opened.count(_LINK) == 1
    assert page.clicked == [craigslist.GO_PASSWORDLESS]
    assert _state(us_seller) == account.ACTIVE
    assert [n["text"] for n in us_seller.list_queued_notices()] == [account.ACTIVATED_NOTICE]


def test_craigslists_terms_are_accepted_on_the_way_in(us_seller, bus, fake) -> None:
    page = FakeCraigslist(terms=True)
    us_seller.request_craigslist_signup()
    us_seller.set_craigslist_awaiting_activation()
    account.record_service_mail(us_seller, _activation_mail())

    account.account_lane(_deps(us_seller, bus, page, fake))

    assert page.clicked == [craigslist.GO_PASSWORDLESS, craigslist.ACCEPT_TERMS]
    assert _state(us_seller) == account.ACTIVE


def test_activation_that_never_arrives_is_reported_once(us_seller, bus, fake) -> None:
    page = FakeCraigslist()
    us_seller.request_craigslist_signup()
    us_seller.set_craigslist_awaiting_activation()
    late = lambda: time.time() + account.ACTIVATION_WAIT_SEC + 60  # noqa: E731

    account.account_lane(_deps(us_seller, bus, page, fake, now=late))
    account.account_lane(_deps(us_seller, bus, page, fake, now=late))

    assert page.opened == []
    assert [n["text"] for n in us_seller.list_queued_notices()] == [account.ACTIVATION_LATE_NOTICE]


def test_with_no_account_asked_for_the_lane_touches_nothing(us_seller, bus, fake) -> None:
    page = FakeCraigslist()

    account.account_lane(_deps(us_seller, bus, page, fake))

    assert page.opened == []


def test_a_mail_read_again_after_its_link_was_opened_does_not_reopen_it(
    us_seller, bus, fake
) -> None:
    page = FakeCraigslist()
    us_seller.request_craigslist_signup()
    us_seller.set_craigslist_awaiting_activation()
    account.record_service_mail(us_seller, _activation_mail())
    us_seller.mark_craigslist_link_opened(_LINK)

    account.record_service_mail(us_seller, _activation_mail())
    account.account_lane(_deps(us_seller, bus, page, fake))

    assert page.opened == []


def test_an_activation_that_does_not_end_signed_in_starts_over_with_a_notice(
    us_seller, bus, fake
) -> None:
    page = FakeCraigslist()
    page.navigate = lambda url: (page.opened.append(url), setattr(page, "kind", "unknown"))
    us_seller.request_craigslist_signup()
    us_seller.set_craigslist_awaiting_activation()
    account.record_service_mail(us_seller, _activation_mail())

    account.account_lane(_deps(us_seller, bus, page, fake))

    assert us_seller.craigslist_account() is None
    assert [n["text"] for n in us_seller.list_queued_notices()] == [
        account.ACTIVATION_FAILED_NOTICE
    ]


def test_a_link_chrome_could_not_open_is_tried_again_next_tick(us_seller, bus, fake) -> None:
    page = FakeCraigslist()
    real_navigate = page.navigate

    def refuse_once(url):
        page.navigate = real_navigate
        raise BrowserToolError("chrome went away")

    page.navigate = refuse_once
    us_seller.request_craigslist_signup()
    us_seller.set_craigslist_awaiting_activation()
    account.record_service_mail(us_seller, _activation_mail())

    account.account_lane(_deps(us_seller, bus, page, fake))
    account.account_lane(_deps(us_seller, bus, page, fake))

    assert page.opened == [_LINK]
    assert _state(us_seller) == account.ACTIVE
