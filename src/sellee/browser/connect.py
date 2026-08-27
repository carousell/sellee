"""Signing in to a browser marketplace: the shared open-and-probe, and the lane that runs it for
the seller from chat.

Two callers, one implementation. `sellee connect <market>` reaches it through the control route;
a seller who taps **Sign in on desktop** in Telegram reaches it through the lane below. Both put the
market's own page in the agent's Chrome and read back whether the seller is in — we never sign in
for them, and nothing about the session is stored: the cookies in that profile are the state, and
the probe re-derives the answer every time it is asked.

The lane exists because the tap arrives on the provider's receive loop, which answers fast paths
inline. Opening Chrome cold takes seconds to tens of seconds, and blocking that loop would stall
every other message and the typing pulse. So the tap writes a durable row and returns, and this
runs off the row — which also makes a double-tap single-flight (the row's primary key is the
market) and survives a restart between the tap and the open.

Every outcome ends in exactly one notice back to the seller. A request that cannot be served yet
(a pass is driving the tab) is left pending rather than answered wrongly, and a request that has
been pending too long is dropped with a notice rather than retried forever.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from sellee import deployment, marketplaces, settings
from sellee.browser import chrome, foreground, inbox
from sellee.browser import markets as market_adapters
from sellee.browser.client import BrowserError, BrowserUnavailable
from sellee.channel import fastpaths
from sellee.store.browser import CONNECT_MODE_OPEN

log = logging.getLogger(__name__)

# How long a pending request keeps waiting for the browser before it is dropped with a notice.
# Long enough to outlast a publish pass holding the tab; short enough that the seller is not still
# being answered about a tap they have forgotten making.
STALE_REQUEST_SEC = 600.0


class BrowserDown(Exception):
    """The browser could not be driven for a connect/probe request. The control route answers it as
    a 503 with the reason, so the CLI can print the by-hand hint instead of pretending the market is
    signed out; the lane turns it into a notice that says the same thing in chat."""


def open_and_probe(*, store, browser_factory, adapter, bring_tab_forward: bool = False):
    """Put the market's own page in front of the seller and read back whether they are in.

    Held exclusively across the navigate and the probe: the read lane shares this one tab, and a
    probe that ran after it moved on would be answering about a different page.

    `bring_tab_forward` selects the tab within the agent's window and is set only when the seller
    asked to sign in: being asked to open a marketplace is being asked for a window, while a read
    probe that reordered tabs would elbow the seller mid-browse.

    Returns (state, url) where state is logged_in | logged_out | unknown.
    """
    region = store.seller_region()
    url = marketplaces.market_home(adapter.market, region)
    if url is None:
        raise BrowserDown(
            f"{marketplaces.display_name(adapter.market)} has no site for "
            f"{region or 'an unset region'}"
        )
    try:
        client = browser_factory()
        with client.exclusive():
            client.navigate(url)
            if bring_tab_forward:
                try:
                    client.ensure_frontmost(url)
                except Exception:
                    # A tab that won't come forward must never turn a working sign-in page into a
                    # failure. But bringing one forward selects a tab before it can check which tab
                    # it got, and a select repoints every later call — so a failure can leave the
                    # probe below reading the seller's own page and reporting a login state about
                    # it. Navigating again re-opens a tab of ours and puts the market back in it,
                    # which is what the probe has to be answering about.
                    log.debug("could not bring the connect tab forward", exc_info=True)
                    client.navigate(url)
            answer = client.evaluate(adapter.login_js) or {}
    except BrowserError as exc:
        raise BrowserDown(str(exc)) from exc
    state = answer.get("state")
    return (state if state in ("logged_in", "logged_out") else "unknown"), url


# --- the lane -----------------------------------------------------------------------------------

# Copy. Every one of these is read on a phone and acted on at a desktop, so each says where the
# window is rather than assuming the seller is sitting in front of it.
SIGNED_IN_NOTICE = "✅ Signed in to {name} — I'm reading that market again."
SIGN_IN_HERE_NOTICE = (
    "{name}'s sign-in page is open in my Chrome window{where}. Sign in there, then tap Check again."
)
STILL_OUT_NOTICE = (
    "I still see a login screen on {name}. Finish signing in on that tab, then tap Check again."
)
CANT_OPEN_NOTICE = (
    "I couldn't open {name} — {reason}. {chrome_check} Or run `sellee connect {market}` at a "
    "shell{where}."
)
STALE_NOTICE = (
    "I couldn't get to opening {name} — the browser stayed busy. Tap below and I'll try again."
)
NO_ADAPTER_NOTICE = "I don't know how to sign in to {market} — nothing I can open for you."

# Where the agent's Chrome window is. On a host install it is one this machine opened for itself
# and the seller has to tell it apart from their own; in a container it is theirs, on their own
# desktop, started by hand.
WINDOW_HERE = " — a separate window from your usual Chrome; check the Dock if you minimized it"
WINDOW_IN_CONTAINER = " on your own computer (the Chrome you started with start-chrome.sh)"
SHELL_IN_CONTAINER = " in the container"


@dataclass
class ConnectDeps:
    store: object
    bus: object
    config: object
    browser_factory: object
    now: Callable[[], float] = time.time


def _window_where() -> str:
    return WINDOW_IN_CONTAINER if deployment.is_container() else WINDOW_HERE


def _shell_where() -> str:
    """Where the shell that runs the CLI is. Not *how* to get there: which container runtime, and
    what the container is called, are the operator's business and not something we can guess."""
    return SHELL_IN_CONTAINER if deployment.is_container() else ""


def _chrome_check() -> str:
    # The same hint the read lane gives, for the same reason: which Chrome to go and look at
    # depends on which machine it is running on.
    return inbox.CONTAINER_CHROME_CHECK if deployment.is_container() else inbox.CHROME_CHECK


def connect_lane(deps: ConnectDeps) -> None:
    """One tick: serve every pending sign-in request the seller asked for from chat.

    A request is cleared once it has an answer — including an answer the seller won't like ("still
    signed out"), which is an answer. The only thing that leaves a row pending is the browser being
    unavailable *for a reason that passes*: a pass driving the tab. Anything else is reported.
    """
    for request in deps.store.pending_market_connects():
        market = request["market"]
        adapter = market_adapters.get_adapter(market)
        if adapter is None:
            # A market id with no adapter can only come from a stale button or a withdrawn
            # registry entry. Clear it — retrying would never start working.
            deps.store.clear_market_connect_request(market)
            deps.store.queue_notice(NO_ADAPTER_NOTICE.format(market=market))
            continue
        if inbox.browser_pass_running(deps.store):
            # A pass mid-drive owns the tab. Navigating it now would pull the page out from under
            # a half-filled composer, and the seller asked to sign in, not to lose a listing.
            if deps.now() - request["requested_ts"] > STALE_REQUEST_SEC:
                deps.store.clear_market_connect_request(market)
                deps.store.queue_notice(
                    STALE_NOTICE.format(name=marketplaces.display_name(market)),
                    controls=fastpaths.signin_controls(market),
                )
            continue
        _serve(deps, market, adapter, request["mode"])


def _serve(deps: ConnectDeps, market: str, adapter, mode: str) -> None:
    """Open (or just re-probe) one market and tell the seller what came back."""
    name = marketplaces.display_name(market)
    opening = mode == CONNECT_MODE_OPEN
    try:
        state, _url = open_and_probe(
            store=deps.store,
            browser_factory=deps.browser_factory,
            adapter=adapter,
            bring_tab_forward=opening,
        )
    except (BrowserDown, BrowserUnavailable) as exc:
        deps.store.clear_market_connect_request(market)
        deps.store.queue_notice(
            CANT_OPEN_NOTICE.format(
                name=name,
                market=market,
                reason=exc,
                chrome_check=_chrome_check(),
                where=_shell_where(),
            ),
            controls=fastpaths.signin_controls(market),
        )
        return

    deps.store.clear_market_connect_request(market)
    deps.bus.publish("browser.login", {"market": market, "state": state})
    if state == "logged_in":
        _ask_about_existing_listings(deps, market)
        deps.store.queue_notice(SIGNED_IN_NOTICE.format(name=name))
        return
    if opening:
        _raise_window(deps)
    template = SIGN_IN_HERE_NOTICE if opening else STILL_OUT_NOTICE
    deps.store.queue_notice(
        template.format(name=name, where=_window_where()),
        controls=fastpaths.check_again_controls(market),
    )


def _ask_about_existing_listings(deps: ConnectDeps, market: str) -> None:
    """Line up a look at what the seller already has listed here.

    Just a row: reading the listings page is another navigation of the one shared tab, and this is
    the sign-in lane, whose job is to answer the tap it was given. The survey lane picks it up
    within a tick and does the asking. Writing it before the signed-in notice is deliberate — both
    are queued, so the seller reads "you're signed in" first and the listings question after.
    """
    if not market_adapters.can_survey(market, deps.store.seller_region()):
        return
    if deps.store.request_market_survey(market):
        deps.bus.publish("survey.requested", {"market": market, "via": "connect"})


def _raise_window(deps: ConnectDeps) -> None:
    """Bring the agent's Chrome in front of the seller, if that is possible and wanted.

    Best-effort by design, and the notice that follows never claims the window jumped forward — it
    says where to look. Unlike the CLI's raise (which runs from the seller's own frontmost
    terminal, where macOS honors activation) this one runs from the daemon, so the activation may
    simply not land. Chrome is opened and navigated either way, which is the part that matters.
    """
    if deployment.is_container() or not foreground.is_supported():
        return  # the window is on another machine, or this OS cannot raise it
    if not settings.get(deps.store, "raise_browser"):
        return  # the seller asked for the window to stay in the background
    try:
        foreground.raise_window(chrome.resolve_port(deps.config.chrome_cdp_port))
    except Exception:
        log.debug("could not raise the agent's Chrome window", exc_info=True)
