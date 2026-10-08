"""The seller's Craigslist account. The seller creates it, and signs in to it, in sellee's Chrome
window, with the registration address sellee made for them. That address's mail comes to sellee,
so the lane opens Craigslist's emailed links in that window; every click after a link is the
seller's. A seller may sign in with their own account instead: sellee posts from it, but its mail,
buyers' replies included, never reaches sellee, and the seller is told so. The lane works from the
one account row."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from sellee import marketplaces, settings
from sellee.browser import inbox, window
from sellee.browser.client import BrowserError
from sellee.browser.markets import craigslist
from sellee.channel import fastpaths
from sellee.rail.client import RailError
from sellee.store import HOLD_SETUP, HOLD_SIGNIN

log = logging.getLogger(__name__)

# Past this, a link the seller asked for that has not come is worth telling them about.
LINK_WAIT_SEC = 1800.0
# How far the mail's received time may run behind the request's, between two clocks.
CLOCK_SKEW_SEC = 10.0

ACTIVE = "active"
AWAITING_LOGIN_LINK = "awaiting_login_link"

# Anchored on host and path, so a link that merely mentions Craigslist is never recorded.
_ACTIVATION_LINK = re.compile(r"https://accounts\.craigslist\.org/pass\?[^\s<>\"'()]+")
_LOGIN_LINK = re.compile(r"https://accounts\.craigslist\.org/login/onetime\?[^\s<>\"'()]+")

TERMS_LINE = (
    "Posting to Craigslist means accepting its terms of use for each post: "
    "craigslist.org/about/terms.of.use"
)
CONNECT_INTRO = (
    "To connect Craigslist:\n"
    "1. In my Chrome window, create a Craigslist account with the email address I made for you: "
    "{address}. Use this one, not your own: its mail comes to me.\n"
    "2. Craigslist emails that address a link. I'll open it in the same window for you, within "
    "a minute or two, and bring the window forward.\n"
    "3. Finish there: choose a password if you want one, and accept Craigslist's terms.\n\n"
    "Once you're in, I'll post your listings to Craigslist, starting with the ones you have now, "
    "edit them, and answer buyers there for you. " + TERMS_LINE
)
SIGN_BACK_IN_INTRO = (
    "Sign back in to Craigslist in my Chrome window with {address}. If you ask Craigslist to "
    "email a login link, I'll open it in that window for you."
)
STILL_SIGNING_IN_NOTICE = (
    "I still see Craigslist's login screen. If you asked Craigslist to email a login link, I'll "
    "open it in my Chrome window as soon as it arrives, so there's no need to tap again. "
    "Otherwise, finish signing in there, then tap Check again."
)
CONNECT_NOTICE = (
    "Craigslist needs your account before I can post there. Create it at your computer with "
    "`sellee connect craigslist`, or tap Sign in on desktop."
)
SIGNED_OUT_NOTICE = (
    "Craigslist signed me out. Craigslist posts wait until you sign back in at your computer: "
    "`sellee connect craigslist`, or tap Sign in on desktop."
)
LINK_OPENED_NOTICE = (
    "Craigslist emailed your link, and I've opened it in my Chrome window. Finish there; any "
    "password and Craigslist's terms are your choice."
)
LINK_LATE_NOTICE = (
    "Craigslist hasn't emailed the link yet. If you asked for one in my Chrome window, ask again "
    "there."
)
ACTIVATED_NOTICE = (
    "✅ Your Craigslist account is connected. It's yours: tap Sign in myself whenever you want a "
    "login link for your own browser."
)
OWN_ACCOUNT_NOTICE = (
    "✅ Craigslist is connected with your own account, {email}, not the address I made for you. "
    "With your own account:\n"
    "✅ I can post your listings to Craigslist, edit them, and check they stay up.\n"
    "❌ I can't read or answer Craigslist buyers: their replies go to {email}, so answer them "
    "yourself from that inbox.\n"
    "❌ If Craigslist signs me out, its login links go to your inbox, not to me, so you'll sign "
    "back in yourself in my Chrome window.\n\n"
    "To have me answer Craigslist buyers too, sign out in my Chrome window and create an account "
    "with {address}: `sellee connect craigslist`."
)
OWN_SIGN_BACK_IN_INTRO = (
    "Sign back in to Craigslist in my Chrome window with your own account, {email}. Its login "
    "links go to your inbox, so use your password, or open the link in that window."
)
SIGNED_BACK_IN_NOTICE = "✅ You're signed back in to Craigslist, so your posts will go ahead."
SELLER_LOGIN_LINK_NOTICE = (
    "Here's your Craigslist login link. Open it in your own browser within 30 minutes: {link}"
)


def connect_state(
    store, market: str, state: str, *, restart: bool = True, email: str = "", rail_factory=None
) -> str:
    """Record what a Craigslist connect found: signed in settles the account; signed out means
    the seller is about to create it, or sign back in, in sellee's Chrome. A Check again probe
    passes `restart=False`, so it never drops a link mailed to the sign-in already under way.
    `email` is the signed-in account's, which says whether it is the seller's own."""
    if market != marketplaces.CRAIGSLIST:
        return state
    if state == "logged_in":
        _signed_in(store, email=email, rail_factory=rail_factory)
    elif state == "logged_out":
        store.begin_craigslist_sign_in(time.time(), restart=restart)
    return state


def registration_address(rail_factory) -> str:
    """The address sellee made for the seller, or "" when bazaar cannot say right now."""
    try:
        return rail_factory().get_registration_address()
    except RailError as exc:
        log.warning("Craigslist connect could not read the registration address: %s", exc)
        return ""


def connect_intro(store, rail_factory) -> str:
    """What the connect step tells the seller before they sign in, naming their address."""
    address = registration_address(rail_factory) or "your sellee email address"
    row = store.craigslist_account() or {}
    if row.get("state") == AWAITING_LOGIN_LINK:
        if row.get("own_email"):
            return OWN_SIGN_BACK_IN_INTRO.format(email=row["own_email"])
        return SIGN_BACK_IN_INTRO.format(address=address)
    return CONNECT_INTRO.format(address=address)


PROGRESS_WAITING = (
    "⏳ Waiting for you in my Chrome window. After you register, or ask for a login link, "
    "Craigslist emails a link; it takes a minute or so to reach me."
)
PROGRESS_LINK_OPENED = (
    "✓ Craigslist emailed your link, and I've opened it in my Chrome window. Finish there: a "
    "password if you want one, and Craigslist's terms."
)
PROGRESS_CONNECTED = "✅ Craigslist is connected."


def progress(store, rail_factory) -> tuple:
    """(stage, line) of the seller's sign-in, as the terminal follows it: "waiting", "link_opened"
    or "connected". A connected own account's line is what sellee can and cannot do with it."""
    row = store.craigslist_account() or {}
    if row.get("state") == ACTIVE:
        own = row.get("own_email")
        if not own:
            return "connected", PROGRESS_CONNECTED
        address = registration_address(rail_factory) or "the address I made for you"
        return "connected", OWN_ACCOUNT_NOTICE.format(email=own, address=address)
    if row.get("link_opened"):
        return "link_opened", PROGRESS_LINK_OPENED
    return "waiting", PROGRESS_WAITING


def start_login(store, market: str) -> bool:
    """A Craigslist page showed the seller's account signed out: ask them to sign back in at the
    computer. False when there is no account to sign back in to."""
    if market != marketplaces.CRAIGSLIST:
        return False
    row = store.craigslist_account()
    if row is None or row["state"] not in (ACTIVE, AWAITING_LOGIN_LINK):
        return False
    store.mark_craigslist_signed_out(SIGNED_OUT_NOTICE, fastpaths.signin_controls(market))
    return True


DESCRIPTION_NOTICE = (
    "Craigslist won't take a post without a description. Tell me a line or two about "
    "\"{title}\" (what's included, any wear) and I'll post it."
)
ZIP_NOTICE = (
    "Craigslist posts need your ZIP code — it's shown on every post as the item's area. "
    "Tell me yours and I'll start posting there."
)


def hold_post(store, market: str) -> str | None:
    """Why a Craigslist post must wait, or None when it can go ahead: "connect" while the seller
    has no account; "account" while they create it or sign back in; "zip" while they have no ZIP
    code on record."""
    if market != marketplaces.CRAIGSLIST:
        return None
    row = store.craigslist_account()
    if row is None:
        return "connect"
    if row["state"] != ACTIVE:
        return "account"
    if not (store.get_seller_config_section("basics") or {}).get("zip"):
        return "zip"
    return None


def missing_for_post(market: str, item: dict) -> str | None:
    """What an item lacks that Craigslist's form requires: "description", or None."""
    if market != marketplaces.CRAIGSLIST:
        return None
    return None if (item.get("description") or "").strip() else "description"


def record_service_mail(store, mail: dict) -> None:
    """The registration lane's hook for Craigslist's own mail: keep a link the seller asked for in
    sellee's Chrome, or hand one they asked for through Sign in myself to them."""
    text = mail.get("text") or ""
    # Only a link sent after the current request: an older one is not this sign-in's.
    received_ts = _received_ts(mail) + CLOCK_SKEW_SEC
    found = _ACTIVATION_LINK.search(text)
    if found:
        store.record_craigslist_link(found.group(0), received_ts=received_ts)
    found = _LOGIN_LINK.search(text)
    if found and not store.record_craigslist_link(found.group(0), received_ts=received_ts):
        store.hand_seller_login_link(
            found.group(0),
            received_ts=received_ts,
            wait_sec=LINK_WAIT_SEC,
            notice=SELLER_LOGIN_LINK_NOTICE.format(link=found.group(0)),
        )


def _received_ts(mail: dict) -> float:
    try:
        stamp = str(mail.get("received_at") or "").replace("Z", "+00:00")
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return time.time()


def service_hooks(store) -> dict:
    return {marketplaces.CRAIGSLIST: lambda mail: record_service_mail(store, mail)}


@dataclass
class AccountDeps:
    store: object
    bus: object
    browser_factory: Callable
    rail_factory: Callable
    config: object = None
    now: Callable[[], float] = time.time


def account_lane(deps: AccountDeps) -> None:
    """One tick of a sign-in the seller is doing: open the link Craigslist emailed into sellee's
    Chrome, then watch that window until the seller is in."""
    row = deps.store.craigslist_account()
    if row is None or row["state"] == ACTIVE or not row["requested_ts"]:
        return
    if not row["link"] and not row["own_email"]:
        if deps.now() - row["requested_ts"] > LINK_WAIT_SEC:
            deps.store.report_craigslist_late_once(LINK_LATE_NOTICE)
    if _busy(deps):
        return
    try:
        client = deps.browser_factory()
        with client.exclusive():
            if row["link"] and not row["link_opened"]:
                _open_link(deps, client, row["link"])
                return
            # Watched with or without a link: a seller signing in with their own account gets none.
            login = client.evaluate(craigslist.LOGIN_JS) or {}
            if login.get("state") == "logged_in":
                _signed_in(
                    deps.store, email=str(login.get("email") or ""), rail_factory=deps.rail_factory
                )
                deps.bus.publish("craigslist.signed_in", {})
    except (BrowserError, RailError) as exc:
        log.warning("Craigslist account step skipped this tick: %s", exc)


def _open_link(deps: AccountDeps, client, link: str) -> None:
    """Open the link once, in front of the seller; what to click on it is theirs."""
    address = deps.rail_factory().get_registration_address()
    client.navigate_visible(link)
    # Brought forward: the link arrives a minute or so after the seller asked, maybe behind setup.
    if deps.config is not None and settings.get(deps.store, "raise_browser"):
        window.raise_now(getattr(deps.config, "chrome_cdp_port", None))
    deps.store.mark_craigslist_link_opened(link, address, LINK_OPENED_NOTICE)
    deps.bus.publish("craigslist.link_opened", {})


def _busy(deps: AccountDeps) -> bool:
    """A pass owns the tab. The seller's own sign-in, and the setup step it runs inside, are what
    the link belongs to, so neither hold keeps it out."""
    if inbox.browser_pass_running(deps.store):
        return True
    return bool(deps.store.browser_holders() - {HOLD_SIGNIN, HOLD_SETUP})


def _signed_in(store, *, email: str = "", rail_factory=None) -> None:
    own, address = _own_email(email, rail_factory)
    store.craigslist_signed_in(
        activated_notice=ACTIVATED_NOTICE,
        activated_controls=fastpaths.sign_in_myself_controls(),
        back_in_notice=SIGNED_BACK_IN_NOTICE,
        own_email=own,
        own_notice=OWN_ACCOUNT_NOTICE.format(email=own, address=address) if own else "",
    )


def _own_email(email: str, rail_factory) -> tuple:
    """(the account's email, the registration address) when the seller signed in with their own
    email; ("", "") when it is the address sellee made, or either cannot be read."""
    email = email.strip().lower()
    if not email or rail_factory is None:
        return "", ""
    try:
        address = rail_factory().get_registration_address()
    except RailError as exc:
        log.warning("Craigslist sign-in could not read the registration address: %s", exc)
        return "", ""
    return ("", "") if email == address.strip().lower() else (email, address)
