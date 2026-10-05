"""The seller's Craigslist account, which sellee creates itself: its email is the registration
address, whose mail the seller never sees. The lane works from the one account row."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

from sellee import marketplaces
from sellee.browser import inbox
from sellee.browser.client import BrowserError
from sellee.browser.markets import craigslist
from sellee.rail.client import RailError

log = logging.getLogger(__name__)

# Past this, a missing activation mail is worth telling the seller about.
ACTIVATION_WAIT_SEC = 1800.0
# A login link lasts this long, and a newer request voids it, so one is asked for at a time.
LOGIN_WAIT_SEC = 1800.0
# How long a submitted form gets to show its next page; clicks return before it loads.
TRANSITION_WAIT_SEC = 20.0
_POLL_SEC = 0.5

SIGNUP_REQUESTED = "signup_requested"
AWAITING_ACTIVATION = "awaiting_activation"
ACTIVE = "active"
AWAITING_LOGIN_LINK = "awaiting_login_link"
# What connecting answers while sellee creates the account, or signs it back in.
SIGNING_UP = "signing_up"
SIGNING_BACK_IN = "signing_back_in"
AUTOMATIC = (SIGNING_UP, SIGNING_BACK_IN)

# Anchored on host and path, so a link that merely mentions Craigslist is never recorded.
_ACTIVATION_LINK = re.compile(r"https://accounts\.craigslist\.org/pass\?[^\s<>\"'()]+")
_LOGIN_LINK = re.compile(r"https://accounts\.craigslist\.org/login/onetime\?[^\s<>\"'()]+")

SIGNING_UP_NOTICE = (
    "I'm creating your Craigslist account with your sellee email address — "
    "I'll tell you when it's ready. There's no password to set."
)
ACTIVATED_NOTICE = "✅ Your Craigslist account is set up and signed in — no password needed."
SIGN_UP_FAILED_NOTICE = (
    "Craigslist didn't confirm the account I tried to create. "
    "Run `sellee connect craigslist` to try again."
)
ACTIVATION_FAILED_NOTICE = (
    "Craigslist's activation link didn't leave me signed in. "
    "Run `sellee connect craigslist` to start the account again."
)
SIGNING_BACK_IN_NOTICE = (
    "Craigslist signed me out, so I'm signing back in by email. Nothing for you to do."
)
LOGIN_LATE_NOTICE = (
    "Craigslist hasn't sent the login email I asked for, so I've asked again. "
    "Craigslist posts wait until I'm back in."
)
ACTIVATION_LATE_NOTICE = (
    "Craigslist hasn't sent the activation email for your new account yet. "
    "I'll finish setting it up as soon as it arrives."
)


def connect_state(store, market: str, state: str) -> str:
    """What connecting answers when signed out of Craigslist: sellee signs back in to an account
    it made, or creates one, since nobody can sign in to it by hand."""
    if market != marketplaces.CRAIGSLIST or state == "logged_in":
        return state
    if signed_out(store, market):
        return SIGNING_BACK_IN
    store.request_craigslist_signup()
    return SIGNING_UP


def signed_out(store, market: str) -> bool:
    """A Craigslist page showed sellee's account signed out: start a login. False when there is
    no account to sign back in to."""
    if market != marketplaces.CRAIGSLIST:
        return False
    row = store.craigslist_account()
    if row is None or row["state"] not in (ACTIVE, AWAITING_LOGIN_LINK):
        return False
    store.request_craigslist_login()
    return True


ZIP_NOTICE = (
    "Craigslist posts need your ZIP code — it's shown on every post as the item's area. "
    "Tell me yours and I'll start posting there."
)


def hold_post(store, market: str) -> str | None:
    """Why a Craigslist post must wait, or None when it can go ahead: "account" while sellee is
    creating one, asked for here if missing; "zip" while the seller has no ZIP code on record."""
    if market != marketplaces.CRAIGSLIST:
        return None
    row = store.craigslist_account()
    if row is None or row["state"] != ACTIVE:
        if store.request_craigslist_signup():
            store.queue_notice(SIGNING_UP_NOTICE)
        return "account"
    if not (store.get_seller_config_section("basics") or {}).get("zip"):
        return "zip"
    return None


def record_service_mail(store, mail: dict) -> None:
    """The registration lane's hook for Craigslist's own mail: keep an activation or login link."""
    text = mail.get("text") or ""
    found = _ACTIVATION_LINK.search(text)
    if found:
        store.record_craigslist_link(found.group(0), states=(SIGNUP_REQUESTED, AWAITING_ACTIVATION))
    found = _LOGIN_LINK.search(text)
    if found:
        store.record_craigslist_link(found.group(0), states=(AWAITING_LOGIN_LINK,))


def service_hooks(store) -> dict:
    return {marketplaces.CRAIGSLIST: lambda mail: record_service_mail(store, mail)}


@dataclass
class AccountDeps:
    store: object
    bus: object
    browser_factory: Callable
    rail_factory: Callable
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep


def account_lane(deps: AccountDeps) -> None:
    """One tick: sign up when asked, open the activation link once it has arrived, or settle an
    activation a previous tick started and did not finish."""
    row = deps.store.craigslist_account()
    if row is None or row["state"] == ACTIVE:
        return
    if row["state"] == AWAITING_LOGIN_LINK:
        _login_tick(deps, row)
        return
    if row["state"] == AWAITING_ACTIVATION and not row["link"]:
        if deps.now() - row["requested_ts"] > ACTIVATION_WAIT_SEC:
            deps.store.report_craigslist_late_once(ACTIVATION_LATE_NOTICE)
        return
    if inbox.browser_busy(deps.store):
        return
    try:
        client = deps.browser_factory()
        with client.exclusive():
            if not row["link"]:
                _sign_up(deps, client)
            elif row["link_opened"]:
                _settle(deps, client)
            else:
                # A link on a signup_requested row means the sign-up went through.
                deps.store.set_craigslist_awaiting_activation()
                _activate(deps, client, row["link"])
    except (BrowserError, RailError) as exc:
        # Nothing was confirmed either way, so the next tick tries again.
        log.warning("Craigslist account step skipped this tick: %s", exc)


def _page(client) -> str:
    return str((client.evaluate(craigslist.PAGE_JS) or {}).get("kind") or "unknown")


def _next_page(deps: AccountDeps, client, before: str) -> str:
    """The page a submitted form led to, or `before` if it never moved within the deadline."""
    deadline = deps.now() + TRANSITION_WAIT_SEC
    kind = _page(client)
    while kind in (before, "unknown") and deps.now() < deadline:
        deps.sleep(_POLL_SEC)
        kind = _page(client)
    return kind


def _sign_up(deps: AccountDeps, client) -> None:
    address = deps.rail_factory().get_registration_address()
    client.navigate(craigslist.LOGIN_URL)
    kind = _page(client)
    if kind == "login":
        client.type_humanly(craigslist.SIGN_UP_EMAIL, "the sign-up email box", address)
        client.click(craigslist.SIGN_UP_BUTTON, "Create account")
        kind = _next_page(deps, client, "login")
    if kind == "signup_sent":
        deps.store.set_craigslist_awaiting_activation()
        deps.bus.publish("craigslist.signed_up", {})
        return
    deps.store.abandon_craigslist_account(SIGN_UP_FAILED_NOTICE)
    deps.bus.publish("craigslist.sign_up_failed", {})


def _activate(deps: AccountDeps, client, link: str) -> None:
    """Open the link once. Whether it worked is decided by `_settle`, now or on a later tick."""
    client.navigate(link)
    deps.store.mark_craigslist_link_opened(link)
    kind = _page(client)
    if kind == "password_options":
        client.click(craigslist.GO_PASSWORDLESS, "Go Passwordless")
        kind = _next_page(deps, client, "password_options")
    if kind == "terms":
        client.click(craigslist.ACCEPT_TERMS, "I ACCEPT")
        _next_page(deps, client, "terms")
    if (client.evaluate(craigslist.LOGIN_JS) or {}).get("state") == "logged_in":
        _activated(deps)


def _settle(deps: AccountDeps, client) -> None:
    """An opened link that left no active account: read the account page and decide."""
    client.navigate(craigslist.ACCOUNT_URL)
    if (client.evaluate(craigslist.LOGIN_JS) or {}).get("state") == "logged_in":
        _activated(deps)
        return
    # The link is spent, so only a fresh sign-up can finish the account.
    log.warning("Craigslist activation did not end signed in")
    deps.store.abandon_craigslist_account(ACTIVATION_FAILED_NOTICE)
    deps.bus.publish("craigslist.activation_failed", {})


def _activated(deps: AccountDeps) -> None:
    if deps.store.activate_craigslist_account(ACTIVATED_NOTICE):
        deps.bus.publish("craigslist.activated", {})


def _login_tick(deps: AccountDeps, row: dict) -> None:
    """Sign a signed-out account back in: ask for a login link unless one is on its way, open it
    once it arrives, and settle by reading the account page."""
    sent = row["requested_ts"]
    if not row["link"] and sent and deps.now() - sent < LOGIN_WAIT_SEC:
        return
    if inbox.browser_busy(deps.store):
        return
    try:
        client = deps.browser_factory()
        with client.exclusive():
            if not row["link"]:
                if sent:
                    deps.store.report_craigslist_late_once(LOGIN_LATE_NOTICE)
                _ask_for_login_link(deps, client)
            elif row["link_opened"]:
                _settle_login(deps, client)
            else:
                _open_login_link(deps, client, row["link"])
    except (BrowserError, RailError) as exc:
        log.warning("Craigslist login step skipped this tick: %s", exc)


def _ask_for_login_link(deps: AccountDeps, client) -> None:
    address = deps.rail_factory().get_registration_address()
    client.navigate(craigslist.LOGIN_URL)
    if (client.evaluate(craigslist.LOGIN_JS) or {}).get("state") == "logged_in":
        _restored(deps)
        return
    if _page(client) == "login":
        client.type_humanly(craigslist.LOGIN_EMAIL, "the login email box", address)
        client.click(craigslist.LOGIN_LINK_BUTTON, "E-mail a login link")
        if _next_page(deps, client, "login") != "login_link_sent":
            log.warning("Craigslist did not confirm the login link was sent")
    # Counted as sent either way, so a page that did not confirm is retried after the wait.
    deps.store.mark_craigslist_login_requested(deps.now())
    deps.bus.publish("craigslist.login_requested", {})


def _open_login_link(deps: AccountDeps, client, link: str) -> None:
    client.navigate(link)
    deps.store.mark_craigslist_link_opened(link)
    if _page(client) == "terms":
        client.click(craigslist.ACCEPT_TERMS, "I ACCEPT")
        _next_page(deps, client, "terms")
    if (client.evaluate(craigslist.LOGIN_JS) or {}).get("state") == "logged_in":
        _restored(deps)


def _settle_login(deps: AccountDeps, client) -> None:
    """An opened login link that left the account signed out is spent; ask for another."""
    client.navigate(craigslist.ACCOUNT_URL)
    if (client.evaluate(craigslist.LOGIN_JS) or {}).get("state") == "logged_in":
        _restored(deps)
        return
    log.warning("Craigslist login link did not end signed in")
    deps.store.reset_craigslist_login()


def _restored(deps: AccountDeps) -> None:
    if deps.store.restore_craigslist_session():
        deps.bus.publish("craigslist.signed_back_in", {})
