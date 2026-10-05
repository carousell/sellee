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

SIGNUP_REQUESTED = "signup_requested"
AWAITING_ACTIVATION = "awaiting_activation"
ACTIVE = "active"
# What connecting answers while sellee creates the account.
SIGNING_UP = "signing_up"

# Anchored on host and path, so a link that merely mentions Craigslist is never recorded.
_ACTIVATION_LINK = re.compile(r"https://accounts\.craigslist\.org/pass\?[^\s<>\"'()]+")

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
ACTIVATION_LATE_NOTICE = (
    "Craigslist hasn't sent the activation email for your new account yet. "
    "I'll finish setting it up as soon as it arrives."
)


def connect_state(store, market: str, state: str) -> str:
    """What connecting answers: signed out of Craigslist with no account to sign back in to means
    sellee is creating one, since nobody can sign in to it by hand."""
    if market != marketplaces.CRAIGSLIST or state == "logged_in":
        return state
    store.request_craigslist_signup()
    row = store.craigslist_account()
    return SIGNING_UP if row is not None and row["state"] != ACTIVE else state


def record_service_mail(store, mail: dict) -> None:
    """The registration lane's hook for Craigslist's own mail: keep an activation link."""
    found = _ACTIVATION_LINK.search(mail.get("text") or "")
    if found:
        store.record_craigslist_link(found.group(0), state=AWAITING_ACTIVATION)


def service_hooks(store) -> dict:
    return {marketplaces.CRAIGSLIST: lambda mail: record_service_mail(store, mail)}


@dataclass
class AccountDeps:
    store: object
    bus: object
    browser_factory: Callable
    rail_factory: Callable
    now: Callable[[], float] = time.time


def account_lane(deps: AccountDeps) -> None:
    """One tick: sign up when asked, or open the activation link once it has arrived."""
    row = deps.store.craigslist_account()
    if row is None or row["state"] == ACTIVE:
        return
    if row["state"] == AWAITING_ACTIVATION and (not row["link"] or row["link_opened"]):
        if not row["link"] and deps.now() - row["requested_ts"] > ACTIVATION_WAIT_SEC:
            deps.store.report_craigslist_late_once(ACTIVATION_LATE_NOTICE)
        return
    if inbox.browser_busy(deps.store):
        return
    try:
        client = deps.browser_factory()
        with client.exclusive():
            if row["state"] == SIGNUP_REQUESTED:
                _sign_up(deps, client)
            else:
                _activate(deps, client, row["link"])
    except (BrowserError, RailError) as exc:
        # Nothing was confirmed either way, so the next tick tries again.
        log.warning("Craigslist account step skipped this tick: %s", exc)


def _page(client) -> str:
    return str((client.evaluate(craigslist.PAGE_JS) or {}).get("kind") or "unknown")


def _sign_up(deps: AccountDeps, client) -> None:
    address = deps.rail_factory().get_registration_address()
    client.navigate(craigslist.LOGIN_URL)
    if _page(client) == "login":
        client.type_humanly(craigslist.SIGN_UP_EMAIL, "the sign-up email box", address)
        client.click(craigslist.SIGN_UP_BUTTON, "Create account")
    if _page(client) == "signup_sent":
        deps.store.set_craigslist_awaiting_activation()
        deps.bus.publish("craigslist.signed_up", {})
        return
    deps.store.abandon_craigslist_account(SIGN_UP_FAILED_NOTICE)
    deps.bus.publish("craigslist.sign_up_failed", {})


def _activate(deps: AccountDeps, client, link: str) -> None:
    client.navigate(link)
    deps.store.mark_craigslist_link_opened(link)
    try:
        if _page(client) == "password_options":
            client.click(craigslist.GO_PASSWORDLESS, "Go Passwordless")
        if _page(client) == "terms":
            client.click(craigslist.ACCEPT_TERMS, "I ACCEPT")
        state = (client.evaluate(craigslist.LOGIN_JS) or {}).get("state")
    except BrowserError as exc:
        state = f"error: {exc}"
    if state == "logged_in" and deps.store.activate_craigslist_account(ACTIVATED_NOTICE):
        deps.bus.publish("craigslist.activated", {})
        return
    # The link is spent, so only a fresh sign-up can finish the account.
    log.warning("Craigslist activation did not end signed in (%s)", state)
    deps.store.abandon_craigslist_account(ACTIVATION_FAILED_NOTICE)
    deps.bus.publish("craigslist.activation_failed", {})
