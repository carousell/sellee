"""Taking over what the seller was already selling: read their listings once, and ask.

Signing in to a marketplace used to hand the agent an inbox and an account and nothing else.
Everything the seller already had listed there stayed invisible, because a buyer conversation is
adopted only when it names a listing we hold an item for — so a seller who connected Carousell with
a dozen live listings got an agent that answered nobody about any of them.

This lane closes that, and the whole design rests on one fact: **adoption is just item rows.** An
item carrying `listing_urls[market]` is what the read lane joins a conversation to, and one carrying
`listing_urls[carousell-ai]` is what the fan-out lists everywhere else. So there is no new inbox
path, no new publish path and no new reporting for the marketplaces the seller already sells on —
there is a survey, an ask, and an item.

Four phases, each deriving its work from durable rows: this module holds the first (read the
marketplace and ask), `browser/adopt.py` holds the other two (turn a yes into items, and get those
items onto carousell.ai). The seller's answer arrives in between, from a button on the notice queued
here or from the tools the seller conversation can call.

The ask happens once per market. That is the survey row's primary key, not a flag anybody has to
remember to set, and the one way back through it is a seller acting on a list that has gone stale —
which reopens the survey rather than adopting from an old one.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from sellee import marketplaces, settings
from sellee.browser import adopt, inbox, reconcile
from sellee.browser import markets as market_adapters
from sellee.browser.client import BrowserError, BrowserUnavailable
from sellee.channel import fastpaths

log = logging.getLogger(__name__)

# How many unserved looks a market gets before we stop asking for one. An unserved look is a market
# we are signed out of, or a listings page that would not read — never a tick where the browser was
# busy with something else, which costs nothing and is not the market's fault.
SURVEY_MAX_ATTEMPTS = 5

# How long an unanswered ask stays live. An approval has a shelf life because the thing it approves
# does not: a yes tapped against a months-old list would relist whatever had sold in the meantime.
# Expiring is not the end of it — the tap on a stale list reopens the survey and asks again.
DECISION_TTL_SEC = 7 * 24 * 3600

# How many listings the ask names one by one before it summarises the rest. A display bound, not a
# work bound: the answer still covers everything found, and the count in the first line says so.
FOUND_BULLETS = 10

# Copy. Read on a phone, about things the seller put up themselves, so it names them rather than
# counting them — a seller cannot answer "manage 12 listings?" without being told which twelve.
FOUND_NOTICE = (
    "I found {count} you're already selling on {name}:\n{bullets}\n\n"
    "Want me to manage these? I'd answer buyers on them here, and list them on {where} too."
)
ACCEPTED_NOTICE = (
    "On it — I've got your {count} on {name}. I'll answer buyers on them from now on, and start "
    "putting them on {where}; I'll send each link as it goes up. When a buyer makes an offer I'll "
    "check the lowest price you'd take before I decide anything."
)
DECLINED_NOTICE = (
    "Understood — I'll leave your {name} listings alone. Send me a photo whenever you want "
    "something listed properly."
)
STALE_NOTICE = (
    "That list is out of date, so I'd rather not act on it — let me take a fresh look at what you "
    "have on {name} and I'll come back to you."
)
ALREADY_MANAGING_NOTICE = (
    "I've already taken over {count} on {name} and I'm answering buyers on them. Tell me which "
    "ones you'd rather I left and I'll stop."
)


@dataclass
class SurveyDeps:
    store: object
    bus: object
    config: object
    browser_factory: object
    now: Callable[[], float] = time.time


def survey_lane(deps: SurveyDeps) -> None:
    """One tick: retire stale asks, look at any market still owed a look, then adopt and publish.

    Ordered so that a tick does the cheapest honest thing first. The browser-touching phases yield
    entirely while a pass holds the tab — it holds it for minutes, and a survey has waited longer
    than that already.
    """
    if deps.store.is_paused():
        return
    expired = deps.store.expire_stale_decisions(DECISION_TTL_SEC, now=deps.now())
    if expired:
        deps.bus.publish("survey.expired", {"listings": expired})
    if inbox.browser_pass_running(deps.store):
        return
    discover_phase(deps)
    adopt.adopt_phase(deps)
    adopt.rail_publish_phase(deps)


def discover_phase(deps: SurveyDeps) -> None:
    """Serve every market still owed a look at what the seller already has listed."""
    region = deps.store.seller_region()
    for request in deps.store.pending_market_surveys():
        market = request["market"]
        if not market_adapters.can_survey(market, region):
            # An adapter withdrawn, or a seller whose region this marketplace has no site for.
            # No later tick could serve this, so it stops being owed rather than being retried.
            deps.store.abandon_market_survey(market)
            deps.bus.publish("survey.abandoned", {"market": market, "reason": "not_surveyable"})
            continue
        try:
            _survey(deps, market, region)
        except BrowserUnavailable as exc:
            # The layer cannot be driven at all. Every market is equally unreadable and the read
            # lane already tells the seller, so this leaves the row owed and costs no attempt.
            deps.bus.publish("browser.unavailable", {"reason": str(exc)})
            return
        except BrowserError as exc:
            _unserved(deps, market, f"browser error: {exc}")


def _survey(deps: SurveyDeps, market: str, region: str | None) -> None:
    """Read one market's listings and ask about them, or record why we could not."""
    adapter = market_adapters.get_adapter(market)
    url = marketplaces.market_url(market, "my_listings", region)
    client = deps.browser_factory()
    with client.exclusive():
        client.navigate(url)
        login = client.evaluate(adapter.login_js) or {}
        if login.get("state") != "logged_in":
            # Signed out again between the trigger and here. The read lane owns that notice — a
            # second voice saying it, about a survey the seller never asked for, is noise.
            _unserved(deps, market, f"login state {login.get('state')!r}")
            return
        answer = client.evaluate(adapter.my_listings_js)

    if not isinstance(answer, dict) or not isinstance(answer.get("listings"), list):
        # A failure, not an empty page. The difference is the whole reason the artifact answers with
        # an error: an empty list means "you have nothing listed", which is what stops us asking.
        reason = (answer or {}).get("error") if isinstance(answer, dict) else "unreadable"
        _unserved(deps, market, f"listings unreadable: {reason}")
        return

    fresh = _not_already_ours(deps, market, adapter, answer["listings"])
    deps.bus.publish(
        "survey.read",
        {
            "market": market,
            "found": len(answer["listings"]),
            "fresh": len(fresh),
            "active_count": answer.get("active_count"),
            "dropped": answer.get("dropped", 0),
            "unreadable": answer.get("unreadable", 0),
            "truncated": bool(answer.get("truncated")),
        },
    )
    if not answer["listings"] and answer.get("unreadable"):
        # The page had rows and not one of them yielded a listing we could use. That is a read we
        # failed, not a seller with nothing listed — and the difference is permanent, because
        # recording it as an empty survey closes the ask-once guard and no later tick reopens it.
        _unserved(deps, market, f"no usable price on any of {answer['unreadable']} rows")
        return
    if answer.get("truncated"):
        # The reader could not get to the end of the list — rows still loading, or more than one
        # page of them. Asking now would close the ask-once survey on a partial list, so the seller
        # would be asked about some of their listings and never about the rest. Another look costs
        # a tick; a half-truth costs the listings nobody ever hears about again.
        _unserved(
            deps,
            market,
            f"read {len(answer['listings'])} of {answer.get('active_count')} live listings",
        )
        return
    if not fresh:
        # Surveyed, with nothing to ask about. Recorded as done so it is not asked again.
        deps.store.record_survey_result(market, [])
        return

    deps.store.record_survey_result(
        market,
        fresh,
        notice_text=_found_text(deps, market, fresh),
        controls=fastpaths.survey_controls(market),
        ref=f"survey:{market}",
    )
    deps.bus.publish("survey.asked", {"market": market, "listings": len(fresh)})


def _not_already_ours(deps: SurveyDeps, market: str, adapter, listings: list) -> list:
    """Drop the listings we already hold: an item recording this URL, or an earlier look's row.

    The first is what makes a survey safe to run more than once: everything the agent published
    itself is on that page too, and re-adopting it would make a second item for one listing.
    """
    items = deps.store.list_items()
    known = {row["listing_id"] for row in deps.store.list_discovered_listings(market)}
    fresh = []
    for row in listings:
        listing_id = str(row.get("listing_id") or "")
        if not listing_id or listing_id in known:
            continue
        if reconcile.matching_items(listing_id, items, market, adapter.listing_id_pattern):
            continue
        fresh.append(row)
    return fresh


def _found_text(deps: SurveyDeps, market: str, listings: list) -> str:
    name = marketplaces.display_name(market)
    shown = listings[:FOUND_BULLETS]
    bullets = "\n".join(f"• {row['title']} — {row['price_text']}".rstrip(" —") for row in shown)
    if len(listings) > len(shown):
        bullets += f"\n• …and {len(listings) - len(shown)} more"
    return FOUND_NOTICE.format(
        count=_things(len(listings)),
        name=name,
        bullets=bullets,
        where=relist_targets(deps.store, market),
    )


def _things(count: int) -> str:
    return "1 thing" if count == 1 else f"{count} things"


def _listings(count: int) -> str:
    return "1 listing" if count == 1 else f"{count} listings"


def relist_targets(store, market: str) -> str:
    """Where a yes would put these listings, named rather than implied.

    carousell.ai always, plus whatever else the seller has switched on — so the button never
    promises more than it does, and a seller who sells in three places is told all three.

    The marketplace they came from is never named, even when it is switched on. These listings are
    already there; offering to put them where they are reads as a mistake, and the fan-out will not
    do it either — the item records that URL, so the pair never qualifies.
    """
    rail = marketplaces.display_name(marketplaces.RAIL)
    others = [
        marketplaces.display_name(enabled)
        for enabled in settings.crosslist_markets(store)
        if enabled not in (marketplaces.RAIL, market)
    ]
    if not others:
        return rail
    return f"{rail} and {', '.join(others)}"


def accepted_text(store, market: str, count: int) -> str:
    return ACCEPTED_NOTICE.format(
        count=_listings(count),
        name=marketplaces.display_name(market),
        where=relist_targets(store, market),
    )


def declined_text(market: str) -> str:
    return DECLINED_NOTICE.format(name=marketplaces.display_name(market))


def stale_text(market: str) -> str:
    return STALE_NOTICE.format(name=marketplaces.display_name(market))


def already_managing_text(market: str, count: int) -> str:
    return ALREADY_MANAGING_NOTICE.format(
        count=_listings(count), name=marketplaces.display_name(market)
    )


def _unserved(deps: SurveyDeps, market: str, reason: str) -> None:
    """Count a look that could not be served, and stop asking for one once it is clearly not coming.

    No seller notice at any point. Being signed out and being unable to read a market are both
    already the read lane's to report, and this is work the seller never asked for — telling them
    twice about it, in a message they cannot act on, would be worse than the event log.
    """
    attempts = deps.store.bump_survey_attempt(market)
    deps.bus.publish(
        "survey.unserved", {"market": market, "attempts": attempts, "reason": reason[:200]}
    )
    if attempts >= SURVEY_MAX_ATTEMPTS:
        deps.store.abandon_market_survey(market)
        deps.bus.publish("survey.abandoned", {"market": market, "reason": reason[:200]})
