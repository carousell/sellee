"""The fan-out lane: list an item everywhere the seller sells, and say how it went.

A listing goes on carousell.ai first — the flow that talks to the seller does that itself, in the
conversation where they approved it. Everywhere else is this lane's work: it looks for items that
are live on the rail and missing from a marketplace the seller has enabled, and queues a browser
publish for one of them.

Deriving the trigger from stored rows rather than instructing a recipe to fan out is the whole
design. Rail-first becomes a precondition instead of a step a model can skip; the work is idempotent
because a recorded listing URL stops qualifying; and items published before any of this existed are
picked up with no special path.

Two rules keep it from being expensive or surprising:

  * One shot per item and marketplace. Every attempt is a real pass — minutes of browser work and a
    vision-priced token bill — so a settled attempt is never repeated automatically. What is retried
    freely is the cheap part: whether Chrome is up, whether Node is installed.
  * The outcome is reported by the daemon, from the rows the pass wrote. A publish pass has no
    conversation to report into, and asking a model to remember to send a message is how a listing
    went live once with nobody told.

The lane's third phase pushes the cross-links back the other way: an item listed on both the rail
and a browser marketplace gets the browser listing's URL written onto its carousell.ai listing,
where the listing page renders it to buyers. Unlike the enqueue phase, the push is not held by
quiet hours and takes no pacing reserve — it is one cheap API call on our own rail, not visible
activity on the seller's marketplace account — and unlike a publish attempt it retries freely,
because retrying costs one HTTP call and only ever happens while local state disagrees with what
the rail last accepted.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from sellee import marketplaces, settings
from sellee.browser import markets as market_adapters
from sellee.browser import publisher
from sellee.browser.client import BrowserUnavailable
from sellee.engines import pacing as pacing_engine
from sellee.passes import DEFAULT_PUBLISH_MARKET
from sellee.rail.client import RailError, RailUnprovisioned, listing_id_from_url

log = logging.getLogger(__name__)

# The marker that separates a publish the daemon started from one a person ran from the CLI. Only
# ours is reported, because whoever runs a pass by hand is already watching it.
ORIGIN = "crosslist"

NO_BROWSER_NOTICE = (
    "I can't list on {market} because I can't drive a browser here. The carousell.ai listing is "
    "unaffected. Details: {reason}"
)
PUBLISHED_NOTICE = "{item} is now listed on {market}: {url}"
FAILED_NOTICE = (
    "I couldn't list {item} on {market}. Everything else about it is fine, including its "
    "carousell.ai listing. Ask me and I'll have another go at it."
)


@dataclass
class CrosslistDeps:
    store: object
    bus: object
    config: object
    # The daemon's browser acquisition: calling it verifies Node and makes Chrome answer (starting
    # it if a closed window is all that is missing), or raises BrowserUnavailable.
    browser_factory: Callable[[], object]
    # The daemon's rail acquisition: returns a client, or raises RailUnprovisioned when no key is
    # present — in which case no rail listing can exist and the push phase has nothing to do.
    rail_factory: Callable[[], object]
    # Lane state, in process on purpose: it is all notice de-duplication, and a restart re-arming it
    # errs toward telling the seller twice rather than never.
    notified: dict = field(default_factory=dict)
    now: Callable[[], float] = time.time


def _notify_once(deps: CrosslistDeps, key: str, text: str) -> None:
    if deps.notified.get(key):
        return
    deps.notified[key] = True
    deps.store.queue_notice(text)


def _clear_notice(deps: CrosslistDeps, key: str) -> None:
    deps.notified.pop(key, None)


def in_quiet_hours(deps: CrosslistDeps) -> bool:
    """Whether now is inside the seller's quiet window.

    A publish is a burst of visible activity on the seller's own marketplace account, and one that
    starts at 4am is a pattern nobody sells like. Nothing already running is interrupted — the
    window holds the *start* of new work.
    """
    cfg = pacing_engine.resolve(deps.config, settings.quiet_window_minutes(deps.store))
    stamp = time.localtime(deps.now())
    return pacing_engine.in_quiet_window(
        stamp.tm_hour * 60 + stamp.tm_min, cfg.quiet_start_min, cfg.quiet_end_min
    )


def crosslist_lane(deps: CrosslistDeps) -> None:
    """One tick: report the fan-out publishes that have settled, push any cross-links the rail is
    missing, then queue at most one more publish. The push sits before the quiet-hours gate on
    purpose — see the module docstring."""
    report_settled(deps)
    if deps.store.is_paused():
        return
    push_crosslinks(deps)
    if in_quiet_hours(deps):
        return
    enqueue_next(deps)


# --- deciding what to publish ------------------------------------------------------------------


def pending_pairs(deps: CrosslistDeps, index=None) -> list:
    """Every (item, market) the seller has asked for and does not have yet, oldest item first.

    Eligibility is entirely a function of stored rows, which is what makes the lane idempotent and
    makes it backfill: an item listed long before this existed qualifies, and a recorded URL — or a
    single settled attempt — stops it qualifying forever.
    """
    markets = settings.publish_markets(deps.store)
    if not markets:
        return []

    index = deps.store.publish_pass_index() if index is None else index
    attempted = {
        (row["item_id"], row["market"])
        for row in index
        if row["market"] and row["market"] != DEFAULT_PUBLISH_MARKET
    }
    sold = deps.store.sold_item_ids()

    pairs = []
    for item in deps.store.list_items():
        urls = item["listing_urls"]
        if not urls.get(DEFAULT_PUBLISH_MARKET):
            continue  # nothing to fan out from yet: the rail listing comes first
        if item["id"] in sold:
            continue
        for market in markets:
            if urls.get(market) or (item["id"], market) in attempted:
                continue
            pairs.append((item, market))
    return pairs


def publish_in_flight(index) -> bool:
    """Whether a publish is already queued or running. Passes run one at a time, so queueing a
    second only lengthens the wait ahead of the channel conversations the seller is having."""
    return any(row["status"] in ("queued", "running") for row in index)


def enqueue_next(deps: CrosslistDeps) -> str | None:
    """Queue the next fan-out publish, if there is one and the browser can run it."""
    index = deps.store.publish_pass_index()
    if publish_in_flight(index):
        return None
    pairs = pending_pairs(deps, index)
    if not pairs:
        return None
    item, market = pairs[0]
    if not _browser_ready(deps, market):
        return None

    if publisher.can_drive(market):
        # A market whose form we drive ourselves never spawns a pass: the work is deterministic —
        # fill the fields, pick two dropdowns, press Publish — and a model reading a recipe to do it
        # costs about $1.54 a listing and is only as repeatable as its reading. The two paths
        # coexist; which one a market takes is read off whether it has publish selectors.
        _drive_publish(deps, item, market)
        return None

    pass_id = deps.store.enqueue_pass(
        "publish", {"item_id": item["id"], "market": market, "origin": ORIGIN}
    )
    deps.bus.publish(
        "crosslist.queued",
        {"item_id": item["id"], "market": market},
        pass_id=pass_id,
    )
    return pass_id


def _drive_publish(deps: CrosslistDeps, item: dict, market: str) -> None:
    """Put one item on a marketplace by driving its form, and record what happened.

    The whole outcome is decided by which exception comes back, which is why the driver has two.
    `PublishNotAttempted` means nothing was created and the pair stays eligible for the next tick.
    `PublishUnverified` means a listing may exist, so the pair is *retired* rather than retried —
    the seller having one listing we cannot see is recoverable; two is not.

    A publish that reports a listing id records the URL, which is what takes the pair out of
    `pending_pairs` for good and lets a buyer writing about it be joined to this item.
    """
    region = deps.store.seller_region()
    create_url = marketplaces.market_url(market, "sell", region)
    adapter = market_adapters.get_adapter(market)
    if create_url is None or adapter is None:
        return
    # Staged where the browser server is allowed to read from: the media store is outside its roots,
    # and a marketplace that requires a photograph refuses a listing without one.
    photos = publisher.stage_photos(item["id"], item.get("photos") or [])
    try:
        client = deps.browser_factory()
        with client.exclusive():
            outcome = publisher.publish(
                client,
                adapter,
                item,
                create_url=create_url,
                photos=photos,
                listings_url=marketplaces.market_url(market, "my_listings", region),
            )
    except publisher.PublishNotAttempted as exc:
        # Nothing exists. Left eligible: the next tick tries again.
        deps.bus.publish(
            "crosslist.not_attempted",
            {"item_id": item["id"], "market": market, "reason": str(exc)[:200]},
        )
        return
    except publisher.PublishUnverified as exc:
        # Something may exist. Never re-driven.
        deps.store.record_driven_publish(item["id"], market, status="error")
        deps.bus.publish(
            "crosslist.unverified",
            {"item_id": item["id"], "market": market, "reason": str(exc)[:200]},
        )
        return
    except BrowserUnavailable as exc:
        deps.bus.publish("browser.unavailable", {"reason": str(exc)})
        return
    finally:
        # Copies, so dropping them costs nothing and leaving them would grow the state tree one
        # listing at a time. The item's own photographs are untouched in the media store.
        publisher.clear_staged(item["id"])

    deps.store.record_driven_publish(
        item["id"], market, status="done" if outcome.verified else "error"
    )
    if outcome.verified and outcome.url:
        deps.store.record_listing_url(item["id"], market, outcome.url)
    deps.bus.publish(
        "crosslist.published",
        {
            "item_id": item["id"],
            "market": market,
            "listing_id": outcome.listing_id,
            "url": outcome.url,
            "verified": outcome.verified,
            "reason": outcome.reason,
        },
    )


def _browser_ready(deps: CrosslistDeps, market: str) -> bool:
    """Whether a browser publish could actually run right now.

    Acquiring the daemon's browser is the whole check — it verifies Node, starts Chrome when a
    closed window is all that is missing (announcing the window itself), and raises with the
    by-hand command when it cannot. Checked before queueing rather than inside the pass, because a
    pass that cannot reach a browser would spend this pair's one attempt on a condition that fixes
    itself.
    """
    try:
        deps.browser_factory()
    except BrowserUnavailable as exc:
        deps.bus.publish("browser.unavailable", {"reason": str(exc)})
        _notify_once(
            deps,
            "unavailable",
            NO_BROWSER_NOTICE.format(market=marketplaces.display_name(market), reason=exc),
        )
        return False
    _clear_notice(deps, "unavailable")
    return True


# --- reporting what happened -------------------------------------------------------------------


def report_settled(deps: CrosslistDeps) -> int:
    """Tell the seller how each finished fan-out publish went. Returns how many were reported.

    The verdict comes from the item's recorded listing URL, not from the pass's exit code: a pass
    that ended cleanly without recording a URL has left no listing anyone can find, which is a
    failure whatever it reported about itself.
    """
    reported = 0
    for row in deps.store.unreported_crosslist_passes():
        item = deps.store.get_item(row["item_id"]) if row["item_id"] else None
        market = row["market"] or ""
        url = (item or {}).get("listing_urls", {}).get(market)
        title = (item or {}).get("title") or row["item_id"] or "the item"
        market_name = marketplaces.display_name(market)
        if url:
            text = PUBLISHED_NOTICE.format(item=title, market=market_name, url=url)
        else:
            text = FAILED_NOTICE.format(item=title, market=market_name)
        if not deps.store.report_crosslist_pass(row["pass_id"], text, ref=row["item_id"]):
            continue
        reported += 1
        deps.bus.publish(
            "crosslist.reported",
            {"item_id": row["item_id"], "market": market, "url": url, "ok": bool(url)},
            pass_id=row["pass_id"],
        )
    return reported


# --- pushing the cross-links onto the rail listing ----------------------------------------------

# Which rail platform each market's listing URL is filed under. The values are the rail's proto
# enum *names* (protojson accepts them, and they read as themselves in logs and stored markers).
# Deliberately code, not registry data: this changes only when the rail's enum does. It must stay
# injective — the rail rejects a set carrying the same platform twice — and a market with no entry
# here is left out of the pushed set entirely, never sent as an unspecified platform.
MARKET_PLATFORMS = {
    "carousell": "EXTERNAL_PLATFORM_CAROUSELL",
    "fb": "EXTERNAL_PLATFORM_FACEBOOK_MARKETPLACE",
}


def desired_external_urls(listing_urls: dict) -> list:
    """The external-URL set an item's rail listing should carry, platform-sorted: every recorded
    listing URL whose market maps to a rail platform. The rail's own URL is not in the map, so it
    can never point at itself."""
    urls = [
        {"platform": MARKET_PLATFORMS[market], "url": url}
        for market, url in listing_urls.items()
        if market in MARKET_PLATFORMS and url
    ]
    return sorted(urls, key=lambda entry: entry["platform"])


def push_crosslinks(deps: CrosslistDeps) -> int:
    """Write each item's cross-listing URLs onto its rail listing; returns how many were pushed.

    Deterministic bookkeeping, not a recipe step: the desired set derives from where the item
    actually is (its recorded listing URLs), and a push happens only when that differs from what
    the rail last accepted. A set that has emptied is pushed too — present-but-empty replaces the
    rail's whole set with nothing, clearing stale links. Failure is silent-retry: the seller
    cannot act on an unlinked listing, so a RailError is an event and another try next tick.
    """
    try:
        rail = deps.rail_factory()
    except RailUnprovisioned:
        return 0  # no key, so no rail listing exists to link anything onto

    markers = deps.store.crosslink_pushed_urls()
    sold = deps.store.sold_item_ids()
    pushed = 0
    for item in deps.store.list_items():
        if item["id"] in sold:
            continue  # its rail listing is about to be archived; pushing would race the take-down
        rail_id = listing_id_from_url(item["listing_urls"].get(DEFAULT_PUBLISH_MARKET))
        if not rail_id:
            continue
        desired = desired_external_urls(item["listing_urls"])
        desired_json = json.dumps(desired, sort_keys=True)
        marker = markers.get(item["id"])
        if marker == desired_json:
            continue
        if marker is None and not desired:
            continue  # nothing pushed and nothing to push — no call, no marker row
        try:
            rail.update_listing(rail_id, external_urls={"urls": desired})
        except RailError as exc:
            deps.bus.publish("crosslink.push_failed", {"item_id": item["id"], "reason": str(exc)})
            continue
        deps.store.set_crosslink_pushed(item["id"], desired_json)
        deps.bus.publish(
            "crosslink.pushed",
            {
                "item_id": item["id"],
                "listing_id": rail_id,
                "platforms": [entry["platform"] for entry in desired],
            },
        )
        pushed += 1
    return pushed
