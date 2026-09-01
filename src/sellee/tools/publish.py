"""carousell_ai_publish_listing — compose a live listing over the wrapped rail, atomically.

The LLM never composes a price in cents or a listing URL. This tool: loads the item, converts
money in code, attaches whatever photos carousell_ai_upload_photos has already uploaded, calls
the rail's create_listing, verifies the returned URL live (fail-closed), and only then records it
into listing_urls in a store transaction. It is idempotent — an item already carrying a
carousell-ai URL returns that URL and never double-posts. The rail call runs outside any store
transaction, so the DB lock is never held across network I/O.
"""

from __future__ import annotations

from sellee import settings
from sellee.browser.client import BrowserUnavailable
from sellee.engines import pacing as pacing_engine
from sellee.money import to_price_cents
from sellee.rail.client import RailUnprovisioned
from sellee.store import StoreError
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_PUBLISH,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)
from sellee.tools.verify import verify_market_url

_MARKET = "carousell-ai"
# The rail's media-kind discriminator; it refuses an entry without one ("media type must be image").
_MEDIA_TYPE_IMAGE = 1
# Pass statuses that mean an attempt is still coming — a second one would be a duplicate listing.
_UNSETTLED = ("queued", "running")


def _publish(ctx: ToolContext, params: dict) -> dict:
    item_id = params["item_id"]
    item = ctx.store.get_item(item_id)
    if item is None:
        raise ToolError(f"no item with id {item_id!r}")

    existing = item["listing_urls"].get(_MARKET)
    if existing:
        return {"listing_id": None, "url": existing, "already_published": True}

    # A paused agent takes no marketplace action. The idempotent already-published read above is a
    # no-op and stays allowed; a real publish is refused until resume.
    if ctx.store.is_paused():
        raise ToolError("the agent is paused — resume before publishing")

    if item.get("list_price") is None:
        raise ToolError("item has no list price — set one before publishing")
    if not (item.get("currency") or "").strip():
        raise ToolError("item has no currency — set one before publishing")
    try:
        price_cents = to_price_cents(item["list_price"])
    except ValueError as exc:
        raise ToolError(str(exc)) from exc

    # Every outbound marketplace action reserves through the pacing gate first — a publish is a
    # real action on the carousell-ai account, jitter-free (a slow human-paced form), but it still
    # counts against the per-marketplace hourly cap and quiet hours.
    cfg = pacing_engine.resolve(ctx.config, settings.quiet_window_minutes(ctx.store))
    paced = ctx.store.reserve_action(
        marketplace=_MARKET,
        kind="publish",
        cfg=cfg,
        interactive=ctx.session.tier == TIER_ATTENDED,
    )
    if paced["verdict"] != "go":
        raise ToolError(
            f"paced: {paced['verdict']} on {_MARKET} "
            f"({paced['count']}/{paced['cap']} this hour) — retry in "
            f"{int(paced['delay_sec'])}s"
        )

    args = {
        "title": item["title"],
        "description": item["description"] or "",
        "price_cents": price_cents,
        "currency": item["currency"],
    }
    # Only uploaded photos can be attached — a local path means nothing to the rail. Display order
    # is the item's order, so the first photo is the listing's cover. A photo still without an
    # upload reference is skipped rather than blocking the publish; an item with no photos at all
    # publishes fine (the listing flow asks for photos, but the tool does not gate on them).
    media = [
        {"url": photo["uploaded_url"], "type": _MEDIA_TYPE_IMAGE}
        for photo in item["photos"]
        if photo.get("uploaded_url")
    ]
    if media:
        args["media"] = {"urls": media}

    if ctx.rail_factory is None:
        raise ToolError("the carousell.ai rail is not available in this session")
    try:
        rail = ctx.rail_factory()
    except RailUnprovisioned as exc:
        raise ToolError(
            "carousell.ai is not provisioned — run `sellee provision carousell-ai`"
        ) from exc

    try:
        listing = rail.create_listing(args)
        rail.verify_listing_url(listing["url"])  # fail-closed: raises if not live under /listing/
    except RailUnprovisioned as exc:
        raise ToolError(
            "carousell.ai is not provisioned — run `sellee provision carousell-ai`"
        ) from exc
    except Exception as exc:  # RailError subclasses carry caller-safe, secret-free messages
        raise ToolError(str(exc)) from exc

    try:
        ctx.store.record_listing_url(item_id, _MARKET, listing["url"])
    except StoreError as exc:
        raise ToolError(str(exc)) from exc
    return {"listing_id": listing.get("listing_id"), "url": listing["url"]}


register(
    ToolSpec(
        name="carousell_ai_publish_listing",
        description="Publish an item as a live carousell.ai listing and record its verified URL. "
        "Idempotent: an already-published item returns its existing URL.",
        input_schema={
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
            "additionalProperties": False,
        },
        handler=_publish,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED, TIER_PASS_PUBLISH}),
    )
)


def _record_published_listing_url(ctx: ToolContext, params: dict) -> dict:
    """Record where an item went live in the browser.

    The rail's publish tool does its own recording, because the daemon makes the call and sees the
    result. A browser publish is filled by the pass, so nothing daemon-side knows the outcome and
    this is how it comes back.
    """
    market, url = params["market"], params["url"]
    verdict = verify_market_url(ctx, market, url, ctx.store.seller_region())
    if not verdict.get("ok"):
        raise ToolError(f"not a listing URL on {market}: {verdict.get('reason')}")
    try:
        item = ctx.store.record_listing_url(params["item_id"], market, url)
    except StoreError as exc:
        raise ToolError(str(exc)) from exc
    return {"item_id": item["id"], "market": market, "url": url}


register(
    ToolSpec(
        name="record_published_listing_url",
        description="Record the permalink of a listing you published in the browser, after reading "
        "it off the live page. This is what joins a buyer's conversation to the item, so a publish "
        "is not finished until it is recorded. Refuses a URL that is not a listing on that market.",
        input_schema={
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "market": {"type": "string"},
                "url": {"type": "string"},
            },
            "required": ["item_id", "market", "url"],
            "additionalProperties": False,
        },
        handler=_record_published_listing_url,
        tiers=frozenset({TIER_PASS_PUBLISH, TIER_ATTENDED}),
    )
)


def _queued_for(store, item_id: str, market: str) -> bool:
    """Whether a publish of this item to this market is already waiting or running. Without this,
    a seller asking twice would get two live listings for one item — public, on their own account,
    and theirs to delete."""
    return any(
        row["item_id"] == item_id and row["market"] == market and row["status"] in _UNSETTLED
        for row in store.publish_pass_index()
    )


def _queue_marketplace_publish(ctx: ToolContext, params: dict) -> dict:
    """Queue a browser publish the fan-out will not queue by itself.

    The fan-out spends one automatic attempt per item and marketplace, because an attempt is
    minutes of browser work that an automatic retry could burn on a repeat of the same failure.
    But plenty of failures say nothing about the next attempt, and the item is then stranded with
    no way back. This is the way back, and it is the seller's to ask for: every condition the lane
    checks still holds here, and only the one-shot is skipped — a seller asking for another go has
    decided to spend it.

    What it does not skip is the guard against listing the same thing twice, which is the failure
    that would actually cost them something.
    """
    # Imported here rather than at module scope: crosslist reaches passes, which imports this
    # package back for its tier lists.
    from sellee import crosslist

    item_id, market = params["item_id"], params["market"]
    item = ctx.store.get_item(item_id)
    if item is None:
        raise ToolError(f"no item with id {item_id!r}")

    # Enabling a marketplace is an approval-gated settings change, because it lets the agent post
    # publicly as the seller. A publish here rides on that approval rather than standing in for it.
    if market not in settings.publish_markets(ctx.store):
        raise ToolError(f"{market!r} is not an enabled marketplace")

    urls = item["listing_urls"]
    if urls.get(market):
        return {
            "status": "already_listed",
            "item_id": item_id,
            "market": market,
            "url": urls[market],
        }
    if not urls.get(_MARKET):
        raise ToolError("item is not published on carousell.ai")
    if item_id in ctx.store.sold_item_ids():
        return {"status": "sold", "item_id": item_id, "market": market}

    # The same two questions the lane asks, asked the same way, so a publish requested in
    # conversation cannot post onto an unread market or a second copy of something the seller
    # put there themselves.
    region = ctx.store.seller_region()
    if not crosslist.looked_first(ctx.store, market, region):
        return {"status": "looking_first", "item_id": item_id, "market": market}
    if crosslist.already_listed_by_hand(ctx.store, item, market):
        return {"status": "already_there", "item_id": item_id, "market": market}
    if ctx.store.is_paused():
        raise ToolError("the agent is paused — resume before publishing")
    if _queued_for(ctx.store, item_id, market):
        return {"status": "already_queued", "item_id": item_id, "market": market}

    # Checked before queueing, so "I can't drive a browser right now" is answered in the
    # conversation asking for it rather than by a pass that dies seconds later with nobody there.
    if ctx.browser_factory is None:
        raise ToolError("this session has no browser to publish with")
    try:
        ctx.browser_factory()
    except BrowserUnavailable as exc:
        raise ToolError(str(exc)) from exc

    # The same origin marker the lane uses: it means the daemon owes the seller a report, which is
    # as true of a publish they asked for in passing as of one nobody watched at all.
    pass_id = ctx.store.enqueue_pass(
        "publish", {"item_id": item_id, "market": market, "origin": crosslist.ORIGIN}
    )
    ctx.bus.publish("crosslist.queued", {"item_id": item_id, "market": market}, pass_id=pass_id)
    return {"status": "queued", "pass_id": pass_id, "item_id": item_id, "market": market}


register(
    ToolSpec(
        name="queue_marketplace_publish",
        description="Queue a publish of an item to one of the marketplaces the seller has turned "
        "on, for when the automatic cross-post failed and they ask you to try again. The publish "
        "runs in the background and reports its own outcome, so tell the seller it has started — "
        "never that the listing is up. Refuses a marketplace they have not turned on, and will "
        "not queue a second publish of something already listed there or already under way. "
        "Two statuses mean 'not yet, and not an error': looking_first = we have not read what the "
        "seller already has on that marketplace, so posting now could duplicate it — say we are "
        "checking their existing listings first. already_there = they have that item on that "
        "marketplace themselves, so there is nothing to post.",
        input_schema={
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "market": {"type": "string"},
            },
            "required": ["item_id", "market"],
            "additionalProperties": False,
        },
        handler=_queue_marketplace_publish,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_CHANNEL}),
    )
)
