"""Turning a yes into items, and getting those items onto carousell.ai.

Phases two and three of the survey lane (`browser/survey.py` holds the first and the copy). One
listing per tick: a page read plus a set of photographs.

Three things here are load-bearing:

  * The listing page is read again at adoption time, and `active` is checked — not for freshness
    but for safety. A yes can be tapped against a list days old, and by then something on it may
    have sold; the marketplace says so only in its structured data.
  * The item and its marketplace URL are written together by one store call. Apart, a crash between
    them leaves an item no conversation can match, and a retry makes a second.
  * The carousell.ai publish is owed durably: it is skipped when another publish holds the one
    slot, and nothing else could pick it back up. The row remembers; this phase settles it and
    tells the seller.
"""

from __future__ import annotations

import logging

from sellee import crosslist, marketplaces, paths, settings
from sellee.browser import markets as market_adapters
from sellee.browser import photo_fetch, reconcile
from sellee.browser.client import BrowserDetached, BrowserError, BrowserUnavailable
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

# Attempts per listing before it is retired. Every attempt is a page read.
ADOPT_MAX_ATTEMPTS = 3
# carousell.ai publish attempts per listing — a publish is minutes of work and a real token bill.
RAIL_MAX_ATTEMPTS = 3

# Not the fan-out's origin: its failure copy assumes the item already has a carousell.ai listing,
# which this one does not. Our own origin means that sweep closes the row as owing nothing, and
# this phase reports.
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
    if market not in settings.connected_markets(deps.store):
        # Disconnected after the seller accepted these listings. Adopting one now would read their
        # marketplace and create an item for a market they have switched off — so the row is left
        # exactly as it is, still accepted, and reconnecting resumes it. Deliberately not `_fail`:
        # nothing about this listing went wrong, and spending an attempt on it would retire a
        # perfectly good row after three ticks with the market off.
        return
    if row["attempts"] >= ADOPT_MAX_ATTEMPTS:
        # Retired here rather than filtered out of the query: a row whose last attempt committed
        # but whose retirement did not would be unreachable forever, holding up the batch summary.
        # Ordering puts it last, so it never delays a live one.
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
        # Declined or re-asked while we were reading the page. Their answer wins.
        deps.bus.publish(
            "survey.adopt_dropped",
            {"market": market, "listing_id": listing_id, "reason": str(exc)[:200]},
        )
    except (BrowserUnavailable, BrowserDetached) as exc:
        # The whole layer is down, or our own server has lost Chrome; this listing is no more at
        # fault than any other. Left accepted, with no attempt spent, for a tick where the browser
        # is there.
        deps.bus.publish("browser.unavailable", {"reason": str(exc)})
    except BrowserError as exc:
        _retry_or_fail(deps, row, f"browser error: {exc}")
    except StoreError as exc:
        # The row cannot become a valid item — a title or price the page no longer shows. Terminal.
        _fail(deps, row, str(exc))
    finally:
        _summarise_if_drained(deps, market)


# What a marketplace prints instead of a currency code, and what it means. Only symbols that are
# unambiguous on their own are here: a bare "$" is not one — it is USD, SGD, AUD, CAD and more —
# which is exactly why the seller's own recorded currency is the fallback rather than a guess.
_CURRENCY_SYMBOLS = {
    "RM": "MYR",
    "NT$": "TWD",
    "HK$": "HKD",
    "S$": "SGD",
    "Rp": "IDR",
    "₱": "PHP",
    "£": "GBP",
    "€": "EUR",
    "¥": "JPY",
    "₹": "INR",
    "฿": "THB",
    "₫": "VND",
}


def _currency_for(store, detail: dict) -> str:
    """Which currency a listing's price is in.

    The reader answers this when the page prints a code, and marketplaces mostly do not: Facebook
    renders "S$65" for this seller's account and "$65" for a US one. The reader used to scrape
    `/[A-Z]{3}/` out of the price text, which matches "SGD65" and nothing else — so adoption failed
    terminally with "no usable price" for every seller whose marketplace shows a symbol, including
    every US seller, who has no other marketplace at all because Carousell runs no US site.

    So the code is preferred, a known unambiguous symbol comes next, and the seller's own recorded
    currency is the fallback. That last one is not a guess: the price is on their own listing, on
    their own account, in the country they told us they sell in.
    """
    said = str(detail.get("currency") or "").strip().upper()
    if len(said) == 3 and said.isalpha():
        return said
    text = str(detail.get("price_text") or "")
    # Longest first, so "S$" and "HK$" are not shadowed by a bare "$" ever being added here.
    for symbol in sorted(_CURRENCY_SYMBOLS, key=len, reverse=True):
        if symbol in text:
            return _CURRENCY_SYMBOLS[symbol]
    basics = store.get_seller_config_section("basics") or {}
    return str(basics.get("currency") or "").strip().upper()


def _adopt_one(deps, row: dict, adapter) -> None:
    market, listing_id = row["market"], row["listing_id"]
    relist = row["manage"] == MANAGE_RELIST

    # Idempotent re-entry: a crash after the item write leaves the row accepted and the item
    # already recording this listing. Linking, not creating, keeps a retry from making a second.
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
        # Two items already claim this listing. Adopting would make a third; the read lane refuses
        # the same ambiguity — attaching to the wrong item negotiates against the wrong floor.
        _fail(deps, row, f"{len(existing)} items already claim this listing")
        return

    # The same thing, already adopted from another marketplace. One desk on Carousell and Facebook
    # is one desk: it becomes one item carrying both listing URLs, so buyers from either market land
    # on the same row and carousell.ai gets it once. `rail_owed` is false whenever the twin already
    # has a rail listing, which is what stops the second copy.
    twins = reconcile.items_for_same_listing(
        row["title"], deps.store.list_items(), market, deps.store.sold_item_ids()
    )
    if len(twins) == 1:
        item = deps.store.get_item(twins[0])
        owed = relist and not (item or {}).get("listing_urls", {}).get(marketplaces.RAIL)
        deps.store.adopt_discovered_listing(
            market, listing_id, item_id=twins[0], url=row["url"], rail_owed=owed
        )
        deps.bus.publish(
            "survey.adopted",
            {
                "market": market,
                "listing_id": listing_id,
                "item_id": twins[0],
                "linked": True,
                "same_as": "title",
            },
        )
        return
    if twins:
        # Several items share this title, so which one this listing *is* cannot be settled from the
        # page. Left for the seller rather than merged into whichever came first.
        _fail(deps, row, f"{len(twins)} items already have this title on another marketplace")
        return

    # No exact twin, but something that is plausibly the same object worded differently — the
    # seller's "… (Yudkowsky & Soares)" against our "… by Yudkowsky & Soares". Adopting would make a
    # second item for one book, and then a second rail listing for it, and then fan the first one
    # out to the marketplace this listing is already on. Refused rather than merged: the loose rule
    # is good enough to withhold on and never good enough to fuse two rows with.
    close = [
        candidate["id"]
        for candidate in deps.store.list_items()
        if candidate["id"] not in deps.store.sold_item_ids()
        and not (candidate.get("listing_urls") or {}).get(market)
        and reconcile.same_thing_loosely(row["title"], candidate.get("title") or "")
    ]
    if close:
        _fail(deps, row, "an item you already have looks like this listing under another name")
        return

    client = deps.browser_factory()
    with client.exclusive():
        client.navigate(row["url"])
        detail = client.evaluate(adapter.listing_detail_js)

    if not isinstance(detail, dict):
        # The page would not read — a different answer from "not for sale".
        _retry_or_fail(deps, row, "the listing page would not read")
        return
    if not detail.get("active"):
        # Sold or taken down since the ask. This check is what makes a late yes safe.
        _fail(deps, row, f"no longer for sale ({detail.get('availability') or 'unknown'})")
        return
    price = detail.get("price")
    currency = _currency_for(deps.store, detail)
    if not price or not currency:
        # carousell.ai refuses an item without both.
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
        # No photos, no relist. The item is still adopted — buyers write to it and we answer — and
        # the seller is told what is missing.
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

    The count orders the queue: `next_adoptable_listing` puts failing listings behind untried ones,
    so one bad page never holds up everything the seller said yes to.
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

    One message per batch, not per listing; the only place the quietly-sold ones get accounted for.
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

    Checked before queueing: quiet hours and the hourly cap refuse a publish without it being the
    listing's fault, and a pass queued into one spends its attempt and looks exactly like a failure.
    Held silently — the row stays owed, and a later tick queues it.
    """
    cfg = pacing_engine.resolve(deps.config, settings.quiet_window_minutes(deps.store))
    verdict = deps.store.peek_action(
        marketplace=marketplaces.RAIL, kind="publish", cfg=cfg, now=deps.now()
    )
    return verdict["verdict"] == "go"


def _settle_queued(deps) -> None:
    """Settle each in-flight publish off the rows it wrote, never off its exit code: a clean pass
    that recorded no URL failed, whatever it said about itself."""
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
    lengthens the wait. The row stays owed — the entire reason it is a column.
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
