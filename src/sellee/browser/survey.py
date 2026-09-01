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
from urllib.parse import urljoin

from sellee import marketplaces, settings
from sellee.browser import adopt, inbox, reconcile
from sellee.browser import markets as market_adapters
from sellee.browser.client import BrowserDetached, BrowserError, BrowserUnavailable
from sellee.channel import fastpaths
from sellee.store import StoreError

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
# Five looks in a row could not be served, so the market stops being asked about. Said out loud,
# with the way back attached: nothing else ever revisits an abandoned survey, and the fan-out will
# not publish to a marketplace it has never read — so silence here is a market that quietly stops
# working with no explanation anywhere.
ABANDONED_NOTICE = (
    "I couldn't read your {name} listings — I tried a few times and kept getting nowhere, so I've "
    "stopped for now. I'm still reading your {name} messages. Tap below when you'd like me to try "
    "again."
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
    if inbox.browser_busy(deps.store):
        return
    discover_phase(deps)
    adopt.adopt_phase(deps)
    adopt.rail_publish_phase(deps)


def discover_phase(deps: SurveyDeps) -> None:
    """Serve every market still owed a look at what the seller already has listed."""
    region = deps.store.seller_region()
    connected = settings.connected_markets(deps.store)
    for request in deps.store.pending_market_surveys():
        market = request["market"]
        if not market_adapters.can_survey(market, region):
            # An adapter withdrawn, or a seller whose region this marketplace has no site for.
            # No later tick could serve this, so it stops being owed rather than being retried.
            # Checked ahead of the connection below because it is the permanent condition of the
            # two: a market nothing could ever survey should retire whether or not it is connected,
            # where being disconnected is a state the seller can reverse in one tap.
            deps.store.abandon_market_survey(market)
            deps.bus.publish("survey.abandoned", {"market": market, "reason": "not_surveyable"})
            continue
        if market not in connected:
            # Disconnected since the look was owed. Left owed rather than abandoned: abandoning is
            # for "no later tick could ever serve this", and reconnecting is precisely a later tick
            # that can — a seller who turns a market off and back on should find the question about
            # their existing listings still waiting, not silently retired while it was off.
            continue
        try:
            _survey(deps, market, region)
        except (BrowserUnavailable, BrowserDetached) as exc:
            # The layer cannot be driven at all, or our own server has lost Chrome. Either way this
            # market is no more at fault than any other, so the row is left owed and costs no
            # attempt. That matters more here than anywhere: this lane ticks every 60 seconds and
            # five unserved looks `abandoned` the market for good, silently and unrepeatably — so a
            # five-minute wedge in the daemon's own subprocess would permanently retire the ask
            # about listings the seller already has, and nothing would ever raise it again.
            deps.bus.publish("browser.unavailable", {"reason": str(exc)})
            return
        except BrowserError as exc:
            _unserved(deps, market, f"browser error: {exc}")


def _follow_to_listings(client, adapter, current: str) -> bool:
    """Take the one hop to the page a seller's listings are actually on, where it is not the one we
    navigated to.

    Facebook's `/marketplace/you/selling` shows the listings and gives them no id, so nothing read
    there could be recorded as a listing URL or joined to a conversation. The ids are on the
    seller's public Marketplace profile, whose address contains their own account id — a fact about
    them rather than about Facebook, so it is read off the page instead of being stored or guessed.

    Answers whether the reader is somewhere it can read from: True for a market that needs no hop,
    True once the hop is made, and False when the link was not there — which the caller reports as
    an unserved survey rather than letting the listings artifact answer "nothing listed" from a page
    that was never the right one.
    """
    if not adapter.my_listings_entry_js:
        return True
    answer = client.evaluate(adapter.my_listings_entry_js) or {}
    target = answer.get("url")
    if not target:
        return False
    client.navigate_visible(urljoin(current, str(target)))
    return True


def _survey(deps: SurveyDeps, market: str, region: str | None) -> None:
    """Read one market's listings and ask about them, or record why we could not."""
    adapter = market_adapters.get_adapter(market)
    url = marketplaces.market_url(market, "my_listings", region)
    client = deps.browser_factory()
    with client.exclusive():
        client.navigate_visible(url)
        login = client.evaluate(adapter.login_js) or {}
        if login.get("state") != "logged_in":
            # Signed out again. The read lane owns that notice; a second voice about a survey the
            # seller never asked for is noise.
            _unserved(deps, market, f"login state {login.get('state')!r}")
            return
        if not _follow_to_listings(client, adapter, url):
            _unserved(deps, market, "could not reach the seller's listings page")
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
    if answer.get("unreadable"):
        # Some rows were on the page and would not parse — a free item, a price line that shifted.
        # `truncated` does not catch this: both readers count a dropped row as read when they
        # compute it, so 14 of 17 with 3 unparseable arrives here claiming to be complete.
        #
        # It matters because a row we could not read leaves no trace anywhere: not on an item, not
        # in `discovered_listings`. The fan-out reads that absence as "the seller does not have this
        # there" and posts a second copy of a listing they already have. So a partial look is not a
        # look, and the ask waits for a page we can read all of.
        _unserved(
            deps,
            market,
            f"{answer['unreadable']} of {answer.get('active_count')} listings would not read",
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
    """Drop the listings we already hold: an item recording this URL, an earlier look's row, or the
    same thing already managed from another marketplace.

    Everything the agent published itself is on that page too; re-adopting it would make a second
    item for one listing. And a seller who cross-lists by hand has the same desk on two
    marketplaces — asking "want me to manage this desk?" a second time, about a desk already being
    managed, reads as though the first answer was lost. The adopt phase still checks for itself,
    because these two run minutes apart and the seller answers in between; this is about what the
    question says.
    """
    items = deps.store.list_items()
    sold = deps.store.sold_item_ids()
    known = {row["listing_id"] for row in deps.store.list_discovered_listings(market)}
    fresh = []
    for row in listings:
        listing_id = str(row.get("listing_id") or "")
        if not listing_id or listing_id in known:
            continue
        if reconcile.matching_items(listing_id, items, market, adapter.listing_id_pattern):
            continue
        twins = reconcile.items_for_same_listing(row.get("title") or "", items, market, sold)
        if twins and _link_twin(deps, market, row, twins):
            continue
        # Either nothing matched, or the match could not be written down. Both go into the ask,
        # because the alternative is a listing that exists on the seller's marketplace and appears
        # in none of our records — not on an item, not in `discovered_listings` — which is precisely
        # the state the fan-out reads as "they do not have this there" and duplicates.
        fresh.append(row)
    return fresh


def _link_twin(deps: SurveyDeps, market: str, row: dict, twins: list) -> bool:
    """Record that an item we already manage is also listed here.

    Recognising the seller's own cross-listing and then writing nothing down was a hole with real
    consequences. The listing was dropped from the ask — right, they are already being helped with
    it — but the item went on carrying no URL for this marketplace, so everything downstream still
    believed it was absent from here. The fan-out believed it hardest: on the first tick after
    Facebook became publishable it found fourteen such items and would have posted thirteen of them
    a second time, each a duplicate of the very listing the survey had just correctly recognised.

    Writing the URL is not a new claim about the seller's intent. It is what we just read off their
    own listings page, live, seconds ago — the same standard `record_listing_url` asks of every
    other caller — and it is what lets a buyer writing about this listing be joined to this item.

    Only ever on a single match, exactly as adoption refuses an ambiguous merge: with two items
    sharing a title there is no way to tell from the page which one this listing is, and guessing
    would put a buyer on the wrong item's floor.

    Answers whether the link was written. A False sends the listing into the ask instead of dropping
    it, which is the difference between "the seller decides" and "nobody has any record of it": a
    row that is neither linked nor asked about exists on their marketplace and in none of our
    tables, and the fan-out reads that as an absence and posts a second copy.
    """
    if len(twins) != 1:
        deps.bus.publish(
            "survey.twin_ambiguous",
            {"market": market, "listing_id": row.get("listing_id"), "items": len(twins)},
        )
        return False
    url = str(row.get("url") or "")
    if not url:
        return False
    try:
        deps.store.record_listing_url(twins[0], market, url)
    except StoreError as exc:
        log.warning("could not link %s to %s: %s", row.get("listing_id"), twins[0], exc)
        return False
    deps.bus.publish(
        "survey.linked",
        {"market": market, "listing_id": row.get("listing_id"), "item_id": twins[0], "url": url},
    )
    return True


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
        for enabled in settings.publish_markets(store)
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


def abandoned_text(market: str) -> str:
    return ABANDONED_NOTICE.format(name=marketplaces.display_name(market))


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
        deps.store.queue_notice(
            abandoned_text(market), controls=fastpaths.look_again_controls(market)
        )
