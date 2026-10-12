"""The seller's Craigslist account: the seller creates it, and signs back in to it, in sellee's
Chrome with the registration address. sellee only opens the links Craigslist mails to that address
while the seller is signing in, and watches the window until they are in."""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.utils import parseaddr
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from tests.conftest import connect_craigslist, seed_setting
from tests.fake_carousell_ai_mcp import FakeRelay, serve

from sellee.browser import craigslist_account as account
from sellee.browser.markets import craigslist
from sellee.channel import fastpaths
from sellee.config import Config
from sellee.rail import registration
from sellee.rail.client import RailClient
from sellee.store import HOLD_SETUP, HOLD_SIGNIN

_FIXTURES = Path(__file__).parent / "fixtures" / "craigslist"
_ADDRESS = "7adtxcmldqu4zyi6vgkhq6nrrq@inbox.carousell.ai"
_LINK = (
    "https://accounts.craigslist.org/pass?key=331764570-83nFXrgvkh7DDTEd2vZgcQxa&userid=410294913"
)
_LOGIN_LINK = "https://accounts.craigslist.org/login/onetime?key=abc123"
_SAFE_LINK = re.compile(r"^https://accounts\.craigslist\.org/(pass|login/onetime)\?\S+$")
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


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def _activation_mail(text: str | None = None, received_ts: float | None = None) -> dict:
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
        "received_at": _stamp(time.time() + 5 if received_ts is None else received_ts),
    }


def _login_mail(received_ts: float) -> dict:
    return {
        "id": f"m-login-{received_ts}",
        "from_email": "robot@craigslist.org",
        "from_domain": "craigslist.org",
        "subject": "craigslist login link",
        "text": f"Use this link to log in: {_LOGIN_LINK}",
        "received_at": _stamp(received_ts),
    }


class FakeChrome:
    """sellee's Chrome as the account lane sees it: it can open a page and read whether the seller
    is signed in, and nothing else. It has no click or type, so a lane that tried either fails."""

    def __init__(self, *, signed_in: bool = False, email: str = ""):
        self.signed_in = signed_in
        self.email = email
        self.opened: list = []

    @contextmanager
    def exclusive(self):
        yield

    def navigate_visible(self, url: str) -> None:
        self.opened.append(url)

    def evaluate(self, script: str):
        assert script == craigslist.LOGIN_JS
        if not self.signed_in:
            return {"state": "unknown"}
        return {"state": "logged_in", "email": self.email}


def _deps(store, bus, chrome, fake, now=None):
    return account.AccountDeps(
        store=store,
        bus=bus,
        browser_factory=lambda: chrome,
        rail_factory=lambda: _rail(fake),
        now=now or time.time,
    )


def _reset(store) -> None:
    with store._db.transaction() as conn:
        conn.execute("DELETE FROM craigslist_account")


def _state(store) -> str | None:
    row = store.craigslist_account()
    return row["state"] if row else None


def _notices(store) -> list:
    return [n["text"] for n in store.list_queued_notices()]


# --- connecting -----------------------------------------------------------------------------------


def test_connecting_signed_out_starts_the_sellers_own_sign_up(us_seller) -> None:
    assert account.connect_state(us_seller, "craigslist", "logged_out") == "logged_out"

    row = us_seller.craigslist_account()
    assert row["state"] == "awaiting_activation" and row["requested_ts"] > 0


def test_connecting_a_known_account_signed_out_starts_the_sellers_sign_back_in(us_seller) -> None:
    connect_craigslist(us_seller)

    account.connect_state(us_seller, "craigslist", "logged_out")

    assert _state(us_seller) == account.AWAITING_LOGIN_LINK


def test_the_intro_names_the_address_what_sellee_does_and_craigslists_terms(
    us_seller, fake
) -> None:
    intro = account.connect_intro(us_seller, lambda: _rail(fake))

    steps = [line for line in intro.splitlines() if line[:2] in ("1.", "2.", "3.")]
    assert len(steps) == 3 and _ADDRESS in steps[0]
    assert "starting with the ones you have now" in intro
    assert intro.endswith(account.TERMS_LINE)


def test_connecting_signed_in_connects_the_account_with_sign_in_myself(us_seller) -> None:
    account.connect_state(us_seller, "craigslist", "logged_in")
    account.connect_state(us_seller, "craigslist", "logged_in")

    assert _state(us_seller) == account.ACTIVE
    [notice] = us_seller.list_queued_notices()
    assert notice["text"] == account.ACTIVATED_NOTICE
    assert [label for label, _ in notice["controls"]] == [fastpaths.SIGN_IN_MYSELF_LABEL]


# --- the links Craigslist mails -------------------------------------------------------------------

_URLS = st.one_of(
    st.just(_LINK),
    st.just(_LOGIN_LINK),
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
def test_only_an_accounts_craigslist_link_is_ever_recorded(store, text) -> None:
    _reset(store)
    store.begin_craigslist_sign_in(time.time())

    account.record_service_mail(store, _activation_mail(text))

    link = store.craigslist_account()["link"]
    assert link is None or _SAFE_LINK.match(link)


def test_a_link_mailed_during_the_sellers_sign_up_is_recorded(us_seller) -> None:
    account.connect_state(us_seller, "craigslist", "logged_out")

    account.record_service_mail(us_seller, _activation_mail())

    assert us_seller.craigslist_account()["link"] == _LINK


@pytest.mark.parametrize("when", ["no_account", "signed_in", "signed_out_unasked", "before"])
def test_a_link_mailed_outside_the_sellers_sign_in_is_never_recorded(us_seller, when) -> None:
    """Nothing is opened unless the seller is signing in at the computer right then."""
    if when == "signed_in":
        connect_craigslist(us_seller)
    elif when == "signed_out_unasked":
        connect_craigslist(us_seller)
        account.start_login(us_seller, "craigslist")
    elif when == "before":
        account.connect_state(us_seller, "craigslist", "logged_out")

    sent = time.time() - 3600 if when == "before" else time.time() + 5
    account.record_service_mail(us_seller, _activation_mail(received_ts=sent))
    account.record_service_mail(us_seller, _login_mail(sent))

    assert (us_seller.craigslist_account() or {}).get("link") is None


# --- the lane -----------------------------------------------------------------------------------


def test_the_lane_opens_the_link_once_in_front_of_the_seller_and_clicks_nothing(
    us_seller, bus, fake
) -> None:
    account.connect_state(us_seller, "craigslist", "logged_out")
    account.record_service_mail(us_seller, _activation_mail())
    chrome = FakeChrome()

    account.account_lane(_deps(us_seller, bus, chrome, fake))
    account.account_lane(_deps(us_seller, bus, chrome, fake))

    assert chrome.opened == [_LINK]
    assert _notices(us_seller) == [account.LINK_OPENED_NOTICE]
    assert us_seller.craigslist_account()["address"] == _ADDRESS
    assert _state(us_seller) == "awaiting_activation"


def test_the_lane_connects_the_account_once_the_seller_is_in(us_seller, bus, fake) -> None:
    account.connect_state(us_seller, "craigslist", "logged_out")
    account.record_service_mail(us_seller, _activation_mail())
    chrome = FakeChrome()
    account.account_lane(_deps(us_seller, bus, chrome, fake))

    chrome.signed_in = True
    account.account_lane(_deps(us_seller, bus, chrome, fake))

    assert _state(us_seller) == account.ACTIVE
    assert _notices(us_seller)[-1] == account.ACTIVATED_NOTICE


def test_a_seller_signing_back_in_is_told_their_posts_go_ahead(us_seller, bus, fake) -> None:
    connect_craigslist(us_seller)
    account.start_login(us_seller, "craigslist")
    account.connect_state(us_seller, "craigslist", "logged_out")
    account.record_service_mail(us_seller, _login_mail(time.time() + 5))
    chrome = FakeChrome()
    account.account_lane(_deps(us_seller, bus, chrome, fake))

    chrome.signed_in = True
    account.account_lane(_deps(us_seller, bus, chrome, fake))

    assert chrome.opened == [_LOGIN_LINK]
    assert _notices(us_seller)[-1] == account.SIGNED_BACK_IN_NOTICE


def test_the_lane_opens_the_link_over_the_sellers_own_sign_in_hold_but_never_a_pass(
    us_seller, bus, fake, monkeypatch
) -> None:
    account.connect_state(us_seller, "craigslist", "logged_out")
    account.record_service_mail(us_seller, _activation_mail())
    us_seller.hold_browser(HOLD_SIGNIN, "signing in to craigslist", 900)
    chrome = FakeChrome()

    monkeypatch.setattr(account.inbox, "browser_pass_running", lambda store: True)
    account.account_lane(_deps(us_seller, bus, chrome, fake))
    assert chrome.opened == []

    monkeypatch.setattr(account.inbox, "browser_pass_running", lambda store: False)
    account.account_lane(_deps(us_seller, bus, chrome, fake))
    assert chrome.opened == [_LINK]


def test_a_link_that_never_comes_is_reported_once(us_seller, bus, fake) -> None:
    account.connect_state(us_seller, "craigslist", "logged_out")
    since = us_seller.craigslist_account()["requested_ts"]
    later = since + account.LINK_WAIT_SEC + 1
    chrome = FakeChrome()

    for _ in range(3):
        account.account_lane(_deps(us_seller, bus, chrome, fake, now=lambda: later))

    assert _notices(us_seller) == [account.LINK_LATE_NOTICE]
    assert chrome.opened == []


# --- signed out -----------------------------------------------------------------------------------


def test_a_signed_out_account_asks_the_seller_once_to_sign_back_in(us_seller, bus, fake) -> None:
    connect_craigslist(us_seller)
    chrome = FakeChrome()

    assert account.start_login(us_seller, "craigslist")
    assert account.start_login(us_seller, "craigslist")
    account.account_lane(_deps(us_seller, bus, chrome, fake))

    [notice] = us_seller.list_queued_notices()
    assert notice["text"] == account.SIGNED_OUT_NOTICE
    assert [label for label, _ in notice["controls"]] == [fastpaths.SIGN_IN_LABEL]
    assert chrome.opened == []


def test_with_no_account_there_is_nothing_to_sign_back_in_to(us_seller) -> None:
    assert not account.start_login(us_seller, "craigslist")
    assert us_seller.craigslist_account() is None


@pytest.mark.parametrize(
    "arrange,zip_code,held",
    [
        ("none", "94103", "connect"),
        ("signing_up", "94103", "account"),
        ("signed_out", "94103", "account"),
        ("connected", None, "zip"),
        ("connected", "94103", None),
    ],
)
def test_what_holds_a_craigslist_post(us_seller, arrange, zip_code, held) -> None:
    if arrange == "signing_up":
        account.connect_state(us_seller, "craigslist", "logged_out")
    elif arrange in ("signed_out", "connected"):
        connect_craigslist(us_seller)
    if arrange == "signed_out":
        account.start_login(us_seller, "craigslist")
    basics = {"region": "US", **({"zip": zip_code} if zip_code else {})}
    us_seller.set_seller_config_section("basics", basics)

    assert account.hold_post(us_seller, "craigslist") == held


# --- the seller signs in to their own account ----------------------------------------------------


def _tap(store, bus, token: str, ref: str) -> str:
    event = {"kind": "action", "text": token, "payload": {"ref": ref, "choice": token}}
    reply, _ = fastpaths.handle_fast_path(store, bus, event)
    return reply


def _with_address(store) -> None:
    connect_craigslist(store)
    with store._db.transaction() as conn:
        conn.execute("UPDATE craigslist_account SET address = ?", (_ADDRESS,))


def _handed(store) -> list:
    return [n["text"] for n in store.list_queued_notices() if _LOGIN_LINK in n["text"]]


def test_sign_in_myself_hands_the_seller_the_next_login_link_once(us_seller, bus) -> None:
    _with_address(us_seller)

    assert _ADDRESS in _tap(us_seller, bus, fastpaths.CB_CL_SIGN_IN_MYSELF, "craigslist")
    account.record_service_mail(us_seller, _login_mail(time.time() + 5))
    account.record_service_mail(us_seller, _login_mail(time.time() + 9))

    assert _handed(us_seller) == [account.SELLER_LOGIN_LINK_NOTICE.format(link=_LOGIN_LINK)]


def test_a_login_link_nobody_asked_for_reaches_no_one(us_seller) -> None:
    _with_address(us_seller)

    account.record_service_mail(us_seller, _login_mail(time.time()))

    assert _handed(us_seller) == []


def test_a_link_for_the_sellers_sign_in_in_sellees_chrome_is_opened_there_not_handed_over(
    us_seller, bus
) -> None:
    _with_address(us_seller)
    _tap(us_seller, bus, fastpaths.CB_CL_SIGN_IN_MYSELF, "craigslist")
    account.start_login(us_seller, "craigslist")
    account.connect_state(us_seller, "craigslist", "logged_out")

    account.record_service_mail(us_seller, _login_mail(time.time() + 5))

    assert us_seller.craigslist_account()["link"] == _LOGIN_LINK
    assert _handed(us_seller) == []


# --- end to end through the registration lane ----------------------------------------------------


def test_the_registration_lane_hands_craigslists_own_mail_to_the_account(us_seller, bus, fake):
    account.connect_state(us_seller, "craigslist", "logged_out")
    mail = _activation_mail()
    fake.add_mail("m1", **{k: v for k, v in mail.items() if k not in ("id", "received_at")})

    registration.registration_lane(
        registration.RegistrationDeps(
            store=us_seller,
            bus=bus,
            config=Config(),
            rail_factory=lambda: _rail(fake),
            service_hooks=account.service_hooks(us_seller),
        )
    )

    assert us_seller.craigslist_account()["link"] == _LINK


def test_check_again_keeps_a_login_link_mailed_to_the_sign_in_under_way(
    us_seller, bus, fake, monkeypatch
) -> None:
    # Live, run 8: each Check again restarted the sign-in, so the link mailed before it was
    # dropped as stale and the seller looped on the login page.
    clock = [1_000_000.0]
    monkeypatch.setattr(account, "time", SimpleNamespace(time=lambda: clock[0]))
    connect_craigslist(us_seller)
    account.start_login(us_seller, "craigslist")
    account.connect_state(us_seller, "craigslist", "logged_out")  # Sign in on desktop
    mailed = clock[0] + 30
    clock[0] += 60
    account.connect_state(us_seller, "craigslist", "logged_out", restart=False)  # Check again

    account.record_service_mail(us_seller, _login_mail(mailed))
    chrome = FakeChrome()
    account.account_lane(_deps(us_seller, bus, chrome, fake, now=lambda: clock[0]))

    assert chrome.opened == [_LOGIN_LINK]


def test_check_again_with_no_sign_in_under_way_starts_one(us_seller) -> None:
    connect_craigslist(us_seller)

    account.connect_state(us_seller, "craigslist", "logged_out", restart=False)

    row = us_seller.craigslist_account()
    assert row["state"] == account.AWAITING_LOGIN_LINK and row["requested_ts"] > 0


def test_the_lane_opens_the_link_while_setup_holds_the_window_for_the_sign_in(
    us_seller, bus, fake
) -> None:
    # Live, run 9: setup held the window for its whole marketplace step, so the activation link
    # was never opened while setup waited for the seller to finish signing in.
    account.connect_state(us_seller, "craigslist", "logged_out")
    account.record_service_mail(us_seller, _activation_mail())
    us_seller.hold_browser(HOLD_SETUP, "signing in to marketplaces", 900)
    us_seller.hold_browser(HOLD_SIGNIN, "signing in to craigslist", 900)
    chrome = FakeChrome()

    account.account_lane(_deps(us_seller, bus, chrome, fake))

    assert chrome.opened == [_LINK]


def test_any_other_hold_keeps_the_link_out(us_seller, bus, fake) -> None:
    account.connect_state(us_seller, "craigslist", "logged_out")
    account.record_service_mail(us_seller, _activation_mail())
    us_seller.hold_browser("post:item_1", "closing a craigslist post", 900)
    chrome = FakeChrome()

    account.account_lane(_deps(us_seller, bus, chrome, fake))

    assert chrome.opened == []


# --- the seller's own account ---------------------------------------------------------------

_OWN = "owen@example.com"


def _notice_rows(store) -> list:
    return list(store.list_queued_notices())


def test_a_seller_signing_up_with_their_own_account_is_connected_and_told_its_limits(
    us_seller, bus, fake
) -> None:
    account.connect_state(us_seller, "craigslist", "logged_out")  # no link ever comes
    chrome = FakeChrome(signed_in=True, email=_OWN)

    account.account_lane(_deps(us_seller, bus, chrome, fake))

    row = us_seller.craigslist_account()
    assert row["state"] == account.ACTIVE and row["own_email"] == _OWN
    [notice] = _notice_rows(us_seller)
    assert _OWN in notice["text"] and _ADDRESS in notice["text"]
    assert "can't read or answer Craigslist buyers" in notice["text"]
    assert not notice.get("controls")  # no Sign in myself: it is already theirs
    assert chrome.opened == []


@pytest.mark.parametrize("email", [_ADDRESS, _ADDRESS.upper(), ""])
def test_the_sellee_address_or_an_unread_email_connects_as_before(
    us_seller, bus, fake, email
) -> None:
    account.connect_state(us_seller, "craigslist", "logged_out")
    account.account_lane(_deps(us_seller, bus, FakeChrome(signed_in=True, email=email), fake))

    assert us_seller.craigslist_account()["own_email"] is None
    assert _notices(us_seller) == [account.ACTIVATED_NOTICE]


def test_a_connect_that_finds_the_sellers_own_account_signed_in_says_so(us_seller, fake) -> None:
    account.connect_state(
        us_seller, "craigslist", "logged_in", email=_OWN, rail_factory=lambda: _rail(fake)
    )

    assert us_seller.craigslist_account()["own_email"] == _OWN
    assert _OWN in _notices(us_seller)[-1]


def test_switching_between_own_and_sellee_accounts_tells_the_seller_each_time(
    us_seller, fake
) -> None:
    rail = lambda: _rail(fake)  # noqa: E731
    account.connect_state(us_seller, "craigslist", "logged_in", email=_OWN, rail_factory=rail)
    account.connect_state(us_seller, "craigslist", "logged_in", email=_OWN, rail_factory=rail)
    account.connect_state(us_seller, "craigslist", "logged_in", email=_ADDRESS, rail_factory=rail)

    texts = _notices(us_seller)
    assert len(texts) == 2 and _OWN in texts[0] and texts[1] == account.ACTIVATED_NOTICE
    assert us_seller.craigslist_account()["own_email"] is None


def test_an_own_account_signed_out_is_asked_back_in_with_its_own_email(us_seller, fake) -> None:
    account.connect_state(
        us_seller, "craigslist", "logged_in", email=_OWN, rail_factory=lambda: _rail(fake)
    )
    account.start_login(us_seller, "craigslist")

    intro = account.connect_intro(us_seller, lambda: _rail(fake))

    assert intro == account.OWN_SIGN_BACK_IN_INTRO.format(email=_OWN)


def test_an_own_account_is_never_told_a_login_link_is_late(us_seller, bus, fake) -> None:
    account.connect_state(
        us_seller, "craigslist", "logged_in", email=_OWN, rail_factory=lambda: _rail(fake)
    )
    account.start_login(us_seller, "craigslist")
    account.connect_state(us_seller, "craigslist", "logged_out")
    later = time.time() + account.LINK_WAIT_SEC + 1

    account.account_lane(_deps(us_seller, bus, FakeChrome(), fake, now=lambda: later))

    assert account.LINK_LATE_NOTICE not in _notices(us_seller)


def test_sign_in_myself_on_an_own_account_points_at_their_own_browser(us_seller, bus, fake) -> None:
    account.connect_state(
        us_seller, "craigslist", "logged_in", email=_OWN, rail_factory=lambda: _rail(fake)
    )

    reply = _tap(us_seller, bus, fastpaths.CB_CL_SIGN_IN_MYSELF, "")

    assert reply == fastpaths.SIGN_IN_MYSELF_OWN.format(email=_OWN)


def test_the_opened_link_brings_the_window_forward(us_seller, bus, fake, monkeypatch) -> None:
    # Live, run 10: the link opened behind the seller's terminal, and they took it as nothing.
    raised: list = []
    monkeypatch.setattr(account.window, "raise_now", lambda port: raised.append(port) or True)
    account.connect_state(us_seller, "craigslist", "logged_out")
    account.record_service_mail(us_seller, _activation_mail())
    deps = _deps(us_seller, bus, FakeChrome(), fake)
    deps.config = SimpleNamespace(chrome_cdp_port=9222)

    account.account_lane(deps)

    assert raised == [9222]


# --- the terminal follows the sign-in ---------------------------------------------------------


def test_progress_follows_the_sign_in_from_waiting_to_connected(us_seller, bus, fake) -> None:
    rail = lambda: _rail(fake)  # noqa: E731
    account.connect_state(us_seller, "craigslist", "logged_out")
    assert account.progress(us_seller, rail)[0] == "waiting"

    account.record_service_mail(us_seller, _activation_mail())
    chrome = FakeChrome()
    account.account_lane(_deps(us_seller, bus, chrome, fake))
    assert account.progress(us_seller, rail) == ("link_opened", account.PROGRESS_LINK_OPENED)

    chrome.signed_in = True
    account.account_lane(_deps(us_seller, bus, chrome, fake))
    assert account.progress(us_seller, rail) == ("connected", account.PROGRESS_CONNECTED)


def test_progress_on_an_own_account_says_what_sellee_can_and_cannot_do(us_seller, fake) -> None:
    rail = lambda: _rail(fake)  # noqa: E731
    account.connect_state(us_seller, "craigslist", "logged_in", email=_OWN, rail_factory=rail)

    stage, line = account.progress(us_seller, rail)

    assert stage == "connected" and _OWN in line and "can't read or answer" in line
