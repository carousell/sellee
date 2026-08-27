"""Turning a yes into items, and getting those items onto carousell.ai.

The second and third phases of the survey lane (`browser/survey.py` holds the first and the copy).
One listing per tick each: a listing page read plus a set of photographs is real I/O, and the tick
comes back in a minute.

Three things here are load-bearing and none of them are obvious:

  * **The listing page is read again at adoption time, and `active` is checked.** Not for
    freshness — for safety. A yes can be tapped against a list assembled days ago, and by then
    something on it may have sold. The marketplace says so in its own structured data and nowhere
    a person would see, so this is the only thing standing between a stale tap and relisting
    somebody's sold goods.
  * **The item and its marketplace URL are written together**, by one store call. Apart, a crash
    between them leaves an item no buyer conversation can be matched to, and the retry makes a
    second one for the same listing.
  * **The carousell.ai publish is owed durably.** It is skipped whenever another publish holds the
    one slot, and nothing else in the system could ever pick it back up: the fan-out needs the rail
    listing as its precondition, and `queue_marketplace_publish` refuses a market the seller has not
    enabled. So the row remembers, and this phase is what settles it — including telling the seller
    how it went, in words that fit a listing that has no carousell.ai listing yet.
"""

from __future__ import annotations

import logging

from sellee import crosslist, marketplaces, paths, settings
from sellee.browser import markets as market_adapters
from sellee.browser import photo_fetch, reconcile
from sellee.browser.client import BrowserError, BrowserUnavailable
from sellee.engines import pacing as pacing_engine
from sellee.store import MAX_PHOTOS, StoreError
from sellee.store.survey import (
    LISTING_ACCEPTED,
    MANAGE_RELIST,
    RAIL_DONE,
    RAIL_FAILED,
    RAIL_OWED,
    ListingGone,
)

log = logging.getLogger(__name__)

# How many times one listing is tried before it is given up on. Every attempt is a page read, and a
# page that has not read three times is not going to.
ADOPT_MAX_ATTEMPTS = 3
# How many carousell.ai publishes one adopted listing gets. A publish is minutes of work and a real
# token bill, so this is deliberately small — and a seller who asks gets another go regardless.
RAIL_MAX_ATTEMPTS = 3

# What marks a publish this lane queued. Deliberately *not* the fan-out's origin: that sweep reports
# an outcome in words which assume the item already has a carousell.ai listing ("everything else
# about it is fine, including its carousell.ai listing"), which is precisely what this one does not.
# Carrying our own origin means that sweep closes the row as owing nothing, and this phase reports.
ORIGIN = "adopt"

RELISTED_NOTICE = "{title} is now on {market}: {url}"
NO_PHOTOS_NOTICE = (
    "I've taken over {title} on {name} — I'll answer buyers on it from here. I couldn't bring its "
    "photos across, so it isn't on {rail}; send me a photo and I'll list it there too."
)
RAIL_FAILED_NOTICE = (
    "I couldn't get {title} onto {rail}, so for now it's only on {name}. I'm still answering "
    "buyers on it. Ask me and I'll have another go."
)
SUMMARY_NOTICE = "Done with your {name} listings: {parts}."


# --- phase two: a yes becomes items ---------------------------------------------------------


def adopt_phase(deps) -> None:
    """Adopt one accepted listing, or report why this one never will be."""
    row = deps.store.next_adoptable_listing()
    if row is None:
        return
    market, listing_id = row["market"], row["listing_id"]
    if row["attempts"] >= ADOPT_MAX_ATTEMPTS:
        # Out of attempts, and still sitting in a status nothing else transitions. Retiring it here
        # rather than filtering it out of the query is what makes the count and the retirement
        # survive being interrupted between the two — a row whose capping attempt committed and
        # whose retirement did not would otherwise be unreachable forever, and would hold up the
        # batch summary behind it. The ordering puts it last, so it never delays a live one.
        _fail(deps, row, row["last_error"] or "gave up after repeated failures")
        _summarise_if_drained(deps, market)
        return
    adapter = market_adapters.get_adapter(market)
    if adapter is None:
        _fail(deps, row, "no adapter for this marketplace")
        return
    try:
        _adopt_one(deps, row, adapter)
    except ListingGone as exc:
        # The seller declined it, or asked for a fresh look, while we were reading its page. Their
        # answer wins and nothing is recorded against the listing — there is no row left to record
        # it on.
        deps.bus.publish(
            "survey.adopt_dropped",
            {"market": market, "listing_id": listing_id, "reason": str(exc)[:200]},
        )
    except BrowserUnavailable as exc:
        # The whole layer is down; this listing is no more at fault than any other. Left accepted,
        # with no attempt spent, for a tick where the browser is there.
        deps.bus.publish("browser.unavailable", {"reason": str(exc)})
    except BrowserError as exc:
        _retry_or_fail(deps, row, f"browser error: {exc}")
    except StoreError as exc:
        # The row cannot become a valid item — a title or price the marketplace no longer shows.
        # Terminal: retrying would ask the same page the same question.
        _fail(deps, row, str(exc))
    finally:
        _summarise_if_drained(deps, market)


def _adopt_one(deps, row: dict, adapter) -> None:
    market, listing_id = row["market"], row["listing_id"]
    relist = row["manage"] == MANAGE_RELIST

    # Idempotent re-entry: a crash after the item was written leaves a row still accepted, and the
    # item already records this listing. Linking rather than creating is what keeps a retry from
    # making a second item for one listing.
    existing = reconcile.matching_items(
        listing_id, deps.store.list_items(), market, adapter.listing_id_pattern
    )
    if len(existing) == 1:
        item = deps.store.get_item(existing[0])
        owed = relist and not (item or {}).get("listing_urls", {}).get(marketplaces.RAIL)
        deps.store.adopt_discovered_listing(market, listing_id, item_id=existing[0], rail_owed=owed)
        deps.bus.publish(
            "survey.adopted",
            {"market": market, "listing_id": listing_id, "item_id": existing[0], "linked": True},
        )
        return
    if existing:
        # Two of our items already claim this one listing. That is a data problem, and the read lane
        # refuses to act on the same ambiguity for the same reason — attaching to the wrong item
        # would negotiate against the wrong floor. Adopting here would make a *third*, so it stops.
        _fail(deps, row, f"{len(existing)} items already claim this listing")
        return

    client = deps.browser_factory()
    with client.exclusive():
        client.navigate(row["url"])
        detail = client.evaluate(adapter.listing_detail_js)

    if not isinstance(detail, dict):
        # The page would not read. Not "not for sale" — that is a different answer, and confusing
        # the two would either strand a live listing or adopt a sold one.
        _retry_or_fail(deps, row, "the listing page would not read")
        return
    if not detail.get("active"):
        # It has sold, or been taken down, since the seller was asked. This is the check that makes
        # a yes safe to tap late.
        _fail(deps, row, f"no longer for sale ({detail.get('availability') or 'unknown'})")
        return
    price, currency = detail.get("price"), detail.get("currency")
    if not price or not currency:
        # carousell.ai refuses an item without both, so an item made from this could only ever fail.
        _fail(deps, row, "the listing page shows no usable price")
        return

    photos = _photos(deps, row, detail) if relist else []
    item = deps.store.adopt_discovered_listing(
        market,
        listing_id,
        title=detail.get("title") or row["title"],
        list_price=price,
        currency=currency,
        description=detail.get("description") or "",
        condition=detail.get("condition"),
        photos=photos,
        url=row["url"],
        # A relist with no photographs is not a relist. The item is still worth having — buyers
        # write to it, and we answer — so it is adopted and the seller is told what is missing.
        rail_owed=bool(relist and photos),
    )
    deps.bus.publish(
        "survey.adopted",
        {
            "market": market,
            "listing_id": listing_id,
            "item_id": item["id"],
            "photos": len(photos),
            "relist": relist,
        },
    )
    if relist and not photos:
        deps.store.queue_notice(
            NO_PHOTOS_NOTICE.format(
                title=item["title"],
                name=marketplaces.display_name(market),
                rail=marketplaces.display_name(marketplaces.RAIL),
            )
        )


def _photos(deps, row: dict, detail: dict) -> list:
    """The listing's photographs, brought into the media store. Empty when none could be."""
    urls = [url for url in (detail.get("photo_urls") or []) if isinstance(url, str)]
    if not urls:
        return []
    dest = paths.media_dir() / f"adopted-{row['market']}-{row['listing_id']}"
    return photo_fetch.fetch_listing_photos(
        urls[:MAX_PHOTOS], market=row["market"], dest_dir=dest, referer=row["url"]
    )


def _retry_or_fail(deps, row: dict, reason: str) -> None:
    """Count this listing's attempt, and retire it once it has had its share.

    Counting matters as much as retrying: `next_adoptable_listing` orders by attempts, so a listing
    that keeps failing moves behind every listing that has not been tried — which is what stops one
    bad page holding up everything the seller said yes to.
    """
    attempts = deps.store.bump_listing_attempt(row["market"], row["listing_id"], reason)
    deps.bus.publish(
        "survey.adopt_failed",
        {
            "market": row["market"],
            "listing_id": row["listing_id"],
            "attempts": attempts,
            "reason": reason[:200],
        },
    )
    if attempts >= ADOPT_MAX_ATTEMPTS:
        _fail(deps, row, reason)


def _fail(deps, row: dict, reason: str) -> None:
    deps.store.fail_discovered_listing(row["market"], row["listing_id"], reason)
    deps.bus.publish(
        "survey.listing_dropped",
        {"market": row["market"], "listing_id": row["listing_id"], "reason": reason[:200]},
    )


def _summarise_if_drained(deps, market: str) -> None:
    """Once a market's accepted listings are all settled, say how the batch went — once.

    One message per batch rather than one per listing: a seller who taps yes on twelve listings
    wants to know it happened, not to be told twelve times. It is also the only place the ones that
    had quietly sold get accounted for, which is the half they would otherwise never hear about.
    """
    if any(
        row["status"] == LISTING_ACCEPTED for row in deps.store.list_discovered_listings(market)
    ):
        return
    rows = deps.store.list_discovered_listings(market)
    adopted = sum(1 for row in rows if row["status"] == "adopted")
    dropped = sum(1 for row in rows if row["status"] == "failed")
    if not adopted and not dropped:
        return
    ref = f"survey-summary:{market}:{adopted}:{dropped}"
    if deps.store.has_notice_with_ref(ref):
        return
    parts = []
    if adopted:
        parts.append(f"{adopted} taken over")
    if dropped:
        parts.append(f"{dropped} skipped (already sold, or I couldn't read the page)")
    deps.store.queue_notice(
        SUMMARY_NOTICE.format(name=marketplaces.display_name(market), parts=" and ".join(parts)),
        ref=ref,
    )


# --- phase three: the carousell.ai publish an adopted listing is owed ------------------------


def rail_publish_phase(deps) -> None:
    """Settle the publishes in flight, then start at most one more."""
    _settle_queued(deps)
    if not _publish_would_be_allowed(deps):
        return
    _enqueue_owed(deps)


def _publish_would_be_allowed(deps) -> bool:
    """Whether a carousell.ai publish could actually get through right now.

    Asked before queueing rather than left to the pass, for the same reason the fan-out checks the
    browser before it spends a pair's one attempt: quiet hours and the hourly cap both refuse a
    publish, both fix themselves with time, and neither is the listing's fault. A pass queued into
    one comes back having done nothing and looks exactly like a publish that failed — so the
    attempt is spent, and three of those in a row report an adopted listing as failed when nothing
    was ever wrong with it. Overnight, with the default quiet window, that is every listing the
    seller just said yes to.

    Held silently. There is nothing for the seller to do about the clock, and the row keeps the
    work: it stays owed, and a later tick queues it.
    """
    cfg = pacing_engine.resolve(deps.config, settings.quiet_window_minutes(deps.store))
    verdict = deps.store.peek_action(
        marketplace=marketplaces.RAIL, kind="publish", cfg=cfg, now=deps.now()
    )
    return verdict["verdict"] == "go"


def _settle_queued(deps) -> None:
    """Read each in-flight publish's outcome off the rows it wrote, never off its exit code.

    A pass that ended cleanly without recording a URL has left no listing anyone can find, which is
    a failure whatever it said about itself — the same rule the fan-out settles on.
    """
    for row in deps.store.listings_awaiting_rail_publish():
        pass_row = deps.store.get_pass(row["rail_pass_id"]) if row["rail_pass_id"] else None
        if pass_row is not None and pass_row["status"] in ("queued", "running"):
            continue
        item = deps.store.get_item(row["item_id"]) if row["item_id"] else None
        url = (item or {}).get("listing_urls", {}).get(marketplaces.RAIL)
        title = (item or {}).get("title") or row["title"]
        if url:
            deps.store.set_rail_publish_state(
                row["market"],
                row["listing_id"],
                RAIL_DONE,
                notice_text=RELISTED_NOTICE.format(
                    title=title,
                    market=marketplaces.display_name(marketplaces.RAIL),
                    url=url,
                ),
            )
            deps.bus.publish(
                "survey.relisted",
                {"market": row["market"], "item_id": row["item_id"], "url": url},
            )
            continue
        if row["rail_attempts"] < RAIL_MAX_ATTEMPTS:
            deps.store.set_rail_publish_state(row["market"], row["listing_id"], RAIL_OWED)
            deps.bus.publish(
                "survey.relist_retry",
                {
                    "market": row["market"],
                    "item_id": row["item_id"],
                    "attempts": row["rail_attempts"],
                },
            )
            continue
        deps.store.set_rail_publish_state(
            row["market"],
            row["listing_id"],
            RAIL_FAILED,
            notice_text=RAIL_FAILED_NOTICE.format(
                title=title,
                rail=marketplaces.display_name(marketplaces.RAIL),
                name=marketplaces.display_name(row["market"]),
            ),
        )
        deps.bus.publish(
            "survey.relist_failed", {"market": row["market"], "item_id": row["item_id"]}
        )


def _enqueue_owed(deps) -> None:
    """Start one owed carousell.ai publish, if the single publish slot is free.

    Held rather than dropped when it is not: passes run one at a time, and queueing a second only
    lengthens the wait ahead of the buyer conversations the seller is having. The row stays owed,
    which is the entire reason it is a column.
    """
    owed = deps.store.listings_owed_rail_publish()
    if not owed:
        return
    if crosslist.publish_in_flight(deps.store.publish_pass_index()):
        return
    row = owed[0]
    item = deps.store.get_item(row["item_id"]) if row["item_id"] else None
    if item is None:
        _fail(deps, row, "the adopted item is gone")
        return
    if item["listing_urls"].get(marketplaces.RAIL):
        # Already there — a publish that landed while we were not looking. Nothing owed.
        deps.store.set_rail_publish_state(row["market"], row["listing_id"], RAIL_DONE)
        return
    pass_id = deps.store.enqueue_pass(
        "publish", {"item_id": item["id"], "market": marketplaces.RAIL, "origin": ORIGIN}
    )
    deps.store.set_rail_publish_queued(row["market"], row["listing_id"], pass_id)
    deps.bus.publish(
        "survey.relist_queued",
        {"market": row["market"], "item_id": item["id"], "attempt": row["rail_attempts"] + 1},
        pass_id=pass_id,
    )
