"""Taking over what the seller was already selling: read their listings once, and ask.

A signed-in marketplace used to hand the agent an inbox and an account and nothing else —
everything the seller already had listed stayed invisible, because a buyer conversation is adopted
only when it names a listing we hold an item for.

The whole design rests on one fact: adoption is just item rows. An item carrying
`listing_urls[market]` is what the read lane joins a conversation to, and one carrying
`listing_urls[carousell-ai]` is what the fan-out lists everywhere else — so there is a survey, an
ask, and an item, and no new path beyond that.

Four phases, each deriving its work from durable rows: this module holds the first (read and ask);
`browser/adopt.py` holds the other two (turn a yes into items, get them onto carousell.ai). The
seller's answer arrives in between, from a button on the notice queued here or from the tools.

The ask happens once per market — the survey row's primary key, not a flag anyone sets. The one
way back through it is a seller acting on a list that has gone stale, which reopens the survey.
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

# Unserved looks before a market is abandoned. Only signed-out or unreadable pages count; a tick
# where the browser was busy costs nothing.
SURVEY_MAX_ATTEMPTS = 5

# An unanswered ask expires: a yes tapped against a months-old list would relist whatever has sold
# since. The tap on an expired list reopens the survey.
DECISION_TTL_SEC = 7 * 24 * 3600

# Listings the ask names one by one before summarising the rest — a display bound, not a work bound.
FOUND_BULLETS = 10

# Copy. Read on a phone, so it names the listings rather than counting them.
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

    The browser-touching phases yield entirely while a pass holds the tab — it holds it for minutes.
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
            # No later tick could serve this — abandon rather than retry.
            deps.store.abandon_market_survey(market)
            deps.bus.publish("survey.abandoned", {"market": market, "reason": "not_surveyable"})
            continue
        try:
            _survey(deps, market, region)
        except BrowserUnavailable as exc:
            # Whole layer down; the read lane already tells the seller. Row stays owed, no attempt
            # spent.
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
            # Signed out again. The read lane owns that notice; a second voice about a survey the
            # seller never asked for is noise.
            _unserved(deps, market, f"login state {login.get('state')!r}")
            return
        answer = client.evaluate(adapter.my_listings_js)

    if not isinstance(answer, dict) or not isinstance(answer.get("listings"), list):
        # A failure, not an empty page: an empty list means "nothing listed", which is what stops
        # the asking.
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
        # Rows, but none usable: a failed read, not "nothing listed" — recording it as empty would
        # close the ask-once guard for good.
        _unserved(deps, market, f"no usable price on any of {answer['unreadable']} rows")
        return
    if answer.get("truncated"):
        # Could not read the whole list. Asking now would close the ask-once survey on a partial
        # list: the seller would be asked about some listings and never the rest.
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

    Everything the agent published itself is on that page too; re-adopting it would make a second
    item for one listing.
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
    promises more than it does. The marketplace they came from is never named: these listings are
    already there, and the fan-out will not put them there either.
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

    No seller notice: signed-out and unreadable are already the read lane's to report, and this is
    work the seller never asked for.
    """
    attempts = deps.store.bump_survey_attempt(market)
    deps.bus.publish(
        "survey.unserved", {"market": market, "attempts": attempts, "reason": reason[:200]}
    )
    if attempts >= SURVEY_MAX_ATTEMPTS:
        deps.store.abandon_market_survey(market)
        deps.bus.publish("survey.abandoned", {"market": market, "reason": reason[:200]})
