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

  * Bounded attempts per item and marketplace. Every attempt is minutes of browser work, so a
    failed publish is retried at most `PUBLISH_MAX_ATTEMPTS` times, spaced out (`_shots_spent`).
    What is retried freely is the cheap part: whether Chrome is up, whether Node is installed.
  * Listing is not held by quiet hours. A listing sits there until someone looks at it, so the hour
    it went up is not what a buyer sees. The window still holds follow-ups and nudges, which land
    in someone's notifications at that hour.
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
from sellee.browser import publisher, reconcile
from sellee.browser.client import BrowserError, BrowserUnavailable
from sellee.engines import pacing as pacing_engine
from sellee.passes import DEFAULT_PUBLISH_MARKET
from sellee.rail.client import RailError, RailUnprovisioned, listing_id_from_url

log = logging.getLogger(__name__)

# The marker that separates a publish the daemon started from one a person ran from the CLI. Only
# ours is reported, because whoever runs a pass by hand is already watching it.
ORIGIN = "crosslist"

# Transient publish refusals get this many goes before the pair is spent; "retryable" without a
# bound is a forever-loop.
MAX_DRIVE_ATTEMPTS = 3

# Attempts per (item, market), and the spacing between them. Bounded because most failures are not
# retriable; spaced because the lane ticks every 30s and three attempts in two minutes is one
# attempt three times.
PUBLISH_MAX_ATTEMPTS = 3
PUBLISH_RETRY_AFTER_SEC = 30 * 60.0

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
    # Consecutive transient publish refusals per (item, market). In process, like the notice
    # dedup beside it, so a restart errs toward one more try.
    attempts: dict = field(default_factory=dict)
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

    Not consulted by this lane (see the module docstring); kept as the one place that resolves the
    window against the pacing config.
    """
    cfg = pacing_engine.resolve(deps.config, settings.quiet_window_minutes(deps.store))
    stamp = time.localtime(deps.now())
    return pacing_engine.in_quiet_window(
        stamp.tm_hour * 60 + stamp.tm_min, cfg.quiet_start_min, cfg.quiet_end_min
    )


def crosslist_lane(deps: CrosslistDeps) -> None:
    """One tick: report the fan-out publishes that have settled, push any cross-links the rail is
    missing, then queue at most one more publish."""
    report_settled(deps)
    if deps.store.is_paused():
        return
    push_crosslinks(deps)
    enqueue_next(deps)


# --- deciding what to publish ------------------------------------------------------------------


def pending_pairs(deps: CrosslistDeps, index=None) -> list:
    """Every (item, market) the seller has asked for and does not have yet, oldest item first.

    Eligibility is entirely a function of stored rows, which is what makes the lane idempotent and
    makes it backfill: an item listed long before this existed qualifies, and a recorded URL — or a
    single settled attempt — stops it qualifying forever.
    """
    region = deps.store.seller_region()
    markets = [
        market
        for market in settings.publish_markets(deps.store)
        if looked_first(deps.store, market, region)
    ]
    if not markets:
        return []

    index = deps.store.publish_pass_index() if index is None else index
    spent = _shots_spent(index, deps.now())
    sold = deps.store.sold_item_ids()
    already_there = {market: _titles_seen_on(deps.store, market) for market in markets}

    pairs = []
    for item in deps.store.list_items():
        urls = item["listing_urls"]
        if not urls.get(DEFAULT_PUBLISH_MARKET):
            continue  # nothing to fan out from yet: the rail listing comes first
        if item["id"] in sold:
            continue
        title = item.get("title") or ""
        for market in markets:
            if urls.get(market) or spent.get((item["id"], market)):
                continue
            if any(reconcile.same_thing_loosely(title, seen) for seen in already_there[market]):
                continue  # the seller already has this one there, by hand
            pairs.append((item, market))
    return pairs


def _shots_spent(index, now: float) -> dict:
    """Which (item, market) pairs are out of attempts for the moment.

    Counted from the pass rows, which are never pruned, so there is no second counter to keep in
    step. A pair gets `PUBLISH_MAX_ATTEMPTS` goes spaced by `PUBLISH_RETRY_AFTER_SEC`. A pair in
    flight counts as spent, so nothing is queued twice.
    """
    attempts: dict = {}
    latest: dict = {}
    for row in index:
        market = row.get("market")
        if not market or market == DEFAULT_PUBLISH_MARKET:
            continue
        key = (row.get("item_id"), market)
        if row.get("status") in ("queued", "running"):
            attempts[key] = attempts.get(key, 0) + PUBLISH_MAX_ATTEMPTS  # in flight: hold it
            continue
        attempts[key] = attempts.get(key, 0) + 1
        finished = row.get("finished_ts") or 0
        latest[key] = max(latest.get(key, 0), finished)
    return {
        key: True
        for key, count in attempts.items()
        if count >= PUBLISH_MAX_ATTEMPTS or (now - latest.get(key, 0)) < PUBLISH_RETRY_AFTER_SEC
    }


def _shots_out(index) -> dict:
    """Which pairs have no attempts left at all — the question reporting asks.

    Not `_shots_spent`: a pair in cooldown still has a go coming, so reporting it as failed would
    announce something that has not finished happening.
    """
    attempts: dict = {}
    for row in index:
        market = row.get("market")
        if not market or market == DEFAULT_PUBLISH_MARKET:
            continue
        if row.get("status") in ("queued", "running"):
            continue
        key = (row.get("item_id"), market)
        attempts[key] = attempts.get(key, 0) + 1
    return {key: True for key, count in attempts.items() if count >= PUBLISH_MAX_ATTEMPTS}


def _titles_seen_on(store, market: str) -> set:
    """What the seller already has on this marketplace, by title, whatever they said about it.

    Read from `discovered_listings`, not item URLs: an item carries a market's URL only once the
    seller agreed to manage it there, and every discovered row counts because the question is what
    exists, not what we manage.
    """
    return {
        reconcile.normalize(row.get("title") or "")
        for row in store.list_discovered_listings(market)
        if row.get("title")
    }


def already_listed_by_hand(store, item: dict, market: str) -> bool:
    """Whether the seller already has this thing on that marketplace, posted themselves.

    Public for the same reason `looked_first` is: the publish tool shares this eligibility rather
    than re-implementing it.
    """
    title = item.get("title") or ""
    if not reconcile.normalize(title):
        return False
    # Loose on purpose: withholding costs one un-cross-listed item the seller can ask for; posting
    # after we said we would not costs a second copy on their account.
    return any(reconcile.same_thing_loosely(title, seen) for seen in _titles_seen_on(store, market))


def looked_first(store, market: str, region) -> bool:
    """Whether we have looked at what the seller already has on this marketplace.

    Publishing to a market we have never read is how a seller ends up with two of everything, so
    the survey comes first — this asks for one rather than waiting on another lane to. A market
    nothing could survey is not held back: there is no look to wait for.
    """
    if not market_adapters.can_survey(market, region):
        return True
    # A pending ask holds the gate: the title match below is whole-string and misses a reworded
    # listing, but the ask carries it, and a yes records its URL properly.
    if store.list_discovered_listings(market, status="pending"):
        return False
    store.request_market_survey(market)
    survey = store.get_market_survey(market)
    # Only `done` counts. `abandoned` means the looks could not be served, so we know less than
    # when we started — opening the gate there is the one moment we can least afford it.
    return bool(survey) and survey["state"] == "done"


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
        # A driven market never spawns a pass: the work is deterministic, and a model reading a
        # recipe to do it costs about $1.54 a listing.
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

    The outcome is decided by which exception comes back. `PublishNotAttempted`: nothing was
    created, the pair stays eligible. `PublishUnverified`: a listing may exist, so the pair is
    retired, never retried — one unseen listing is recoverable, two are not. A success records the
    URL, which retires the pair for good.
    """
    region = deps.store.seller_region()
    create_url = marketplaces.market_url(market, "sell", region)
    adapter = market_adapters.get_adapter(market)
    if create_url is None or adapter is None:
        return
    # Staged where the browser server may read from: the media store is outside its roots.
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
        # A terminal refusal spends the pair's shot immediately; a transient one gets
        # `MAX_DRIVE_ATTEMPTS` goes and then spends it too.
        attempts = deps.attempts.get((item["id"], market), 0) + 1
        deps.attempts[(item["id"], market)] = attempts
        giving_up = not getattr(exc, "retryable", False) or attempts >= MAX_DRIVE_ATTEMPTS
        deps.bus.publish(
            "crosslist.not_attempted",
            {
                "item_id": item["id"],
                "market": market,
                "reason": str(exc)[:200],
                "attempts": attempts,
                "giving_up": giving_up,
            },
        )
        if giving_up:
            # The row is what stops the pair qualifying; the report phase tells the seller.
            deps.store.record_driven_publish(item["id"], market, status="error", origin=ORIGIN)
            deps.attempts.pop((item["id"], market), None)
        return
    except publisher.PublishUnverified as exc:
        # Something may exist. Never re-driven.
        deps.store.record_driven_publish(item["id"], market, status="error", origin=ORIGIN)
        deps.bus.publish(
            "crosslist.unverified",
            {"item_id": item["id"], "market": market, "reason": str(exc)[:200]},
        )
        return
    except BrowserUnavailable as exc:
        deps.bus.publish("browser.unavailable", {"reason": str(exc)})
        return
    except BrowserError as exc:
        # The driver should never leak a bare browser error. If one escapes we cannot tell which
        # side of the commit it came from, so treat it as the dangerous side: retire the pair.
        deps.store.record_driven_publish(item["id"], market, status="error", origin=ORIGIN)
        deps.bus.publish(
            "crosslist.unverified",
            {"item_id": item["id"], "market": market, "reason": f"unexpected: {str(exc)[:180]}"},
        )
        return
    finally:
        # Staged copies only; the item's own photographs stay in the media store.
        publisher.clear_staged(item["id"])

    deps.store.record_driven_publish(
        item["id"], market, status="done" if outcome.verified else "error", origin=ORIGIN
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

    A failure the lane will retry is not announced — only the last attempt speaks, and a success
    always does.
    """
    reported = 0
    index = deps.store.publish_pass_index()
    spent = _shots_out(index)
    # One message per item and market, however many attempts are settling at once.
    announced: set = set()
    for row in deps.store.unreported_crosslist_passes():
        item = deps.store.get_item(row["item_id"]) if row["item_id"] else None
        market = row["market"] or ""
        url = (item or {}).get("listing_urls", {}).get(market)
        title = (item or {}).get("title") or row["item_id"] or "the item"
        market_name = marketplaces.display_name(market)
        if url:
            text = PUBLISHED_NOTICE.format(item=title, market=market_name, url=url)
        elif not spent.get((row["item_id"], market)) or (row["item_id"], market) in announced:
            # Another go is coming, or this pair has already had its say in this sweep. The row is
            # still marked reported so the sweep stays bounded; what is withheld is the message,
            # not the bookkeeping.
            deps.store.report_crosslist_pass(row["pass_id"], None, ref=row["item_id"])
            continue
        else:
            text = FAILED_NOTICE.format(item=title, market=market_name)
            announced.add((row["item_id"], market))
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
