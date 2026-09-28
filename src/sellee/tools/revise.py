"""update_live_listing — change what an item's live listings say, everywhere they are.

An item listed on three marketplaces is four copies of the same facts: the record here, and each
listing page a buyer can see. `update_item` changes the first and nothing else, which is how a
seller could be told "price updated" while every buyer still saw the old one. This tool is the
whole change, in the order it has to happen:

  1. refuse what must not be edited — a sold item, a paused agent, a deal already in flight;
  2. write the record, with the floor clamped under the new price in the same transaction;
  3. queue every browser marketplace the item is live on, for the revise lane to drive;
  4. push carousell.ai inline — one call on our own rail, so it either worked or it did not.

The record is written before the rail is called, which inverts the take-down's rail-first order on
purpose. A take-down that recorded first could leave a live listing nothing watches; an edit that
recorded first leaves, at worst, a rail listing one edit behind — reported to the seller as a
failure they can ask to retry. And the photo upload reads the item's photos from the record, so a
photo edit cannot go rail-first at all.

What comes back is a verdict per marketplace, never one "done": the rail answers now, the browser
markets answer later in their own messages, and the tool's description tells the model to say
exactly that.
"""

from __future__ import annotations

from sellee import marketplaces, settings
from sellee.browser import editor
from sellee.money import to_price_cents
from sellee.rail.client import RailError, listing_id_from_url
from sellee.store import StoreError
from sellee.tools import photos
from sellee.tools.checkout import _rail
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_EDIT,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)

_RAIL = marketplaces.RAIL
# The rail's media-kind discriminator; it refuses an entry without one.
_MEDIA_TYPE_IMAGE = 1
# Negotiation states in which moving the price changes a deal someone is already inside. A price
# drop below list rewrites the front-runner and flips the item into bidding; a drop to a held
# offer's amount consumes the hold and closes the sale. Neither should happen as a side effect.
_DEAL_IN_FLIGHT = ("bidding", "reserved_provisional", "sold")
# A buyer whose offer still stands, for counting offers the new price undercuts.
_STANDING = ("passed", "lost")


def _update_live_listing(ctx: ToolContext, params: dict) -> dict:
    item_id, fields = params["item_id"], dict(params["fields"])
    item = ctx.store.get_item(item_id)
    if item is None:
        raise ToolError(f"no item with id {item_id!r}")
    if ctx.store.is_paused():
        raise ToolError("the agent is paused — resume before changing a listing")
    if item_id in ctx.store.sold_item_ids():
        raise ToolError("this item has sold — there is no live listing to change")
    if not fields:
        raise ToolError("no fields to change")

    status = ctx.store.negotiate_status(item_id)
    price_moves = "list_price" in fields and fields["list_price"] != item.get("list_price")
    if price_moves and status["item_state"] in _DEAL_IN_FLIGHT:
        raise ToolError(
            f"a deal is in flight on this item ({status['item_state']}) — changing the price now "
            "would change it underneath the buyer. Release it with negotiate_release first, or "
            "let it finish."
        )
    offers_above = _offers_above(status, fields["list_price"]) if price_moves else 0

    fields = _carry_uploads(item, fields)
    try:
        revised = ctx.store.revise_item(item_id, fields)
    except StoreError as exc:
        raise ToolError(str(exc)) from exc
    item = revised["item"]
    changed = sorted(fields)

    # The browser markets are queued before the rail is called: queueing is local bookkeeping and
    # can fail, a rail push is public and cannot be taken back. In this order a failure here
    # leaves no marketplace changed; in the other it left carousell.ai edited and the seller told
    # "internal error".
    markets: dict = {}
    for market, url in sorted(item["listing_urls"].items()):
        if market == _RAIL or not url or marketplaces.connector_type(market) != "browser":
            continue
        markets[market] = _queue_browser(ctx, item_id, market, url, changed)
    if item["listing_urls"].get(_RAIL):
        markets = {_RAIL: _push_rail(ctx, item, changed), **markets}

    return {
        "item_id": item_id,
        "changed": changed,
        "floor_lowered": revised["floor_clamped"],
        "offers_above_new_price": offers_above,
        "markets": markets,
    }


def _offers_above(status: dict, new_price) -> int:
    """How many standing offers the new price undercuts — a count, so the seller can be told a
    buyer already offered more without the model reasoning about the ledger itself."""
    try:
        to_price_cents(new_price)
    except ValueError:
        return 0  # the store refuses it a moment later, with the reason
    return sum(
        1
        for buyer in status["buyers"].values()
        if buyer.get("status") not in _STANDING and (buyer.get("highest_offer") or 0) > new_price
    )


def _carry_uploads(item: dict, fields: dict) -> dict:
    """Keep a photo's upload reference when the new set still contains that photo.

    The model names photos by path, and a bare path carries no `uploaded_url` — so without this,
    reordering two photos would discard both references and re-upload everything. A path that is
    genuinely new stays bare, which is what marks it for upload.
    """
    if "photos" not in fields or not isinstance(fields["photos"], list):
        return fields
    known = {p["path"]: p.get("uploaded_url") for p in item.get("photos") or [] if p.get("path")}
    carried = []
    for entry in fields["photos"]:
        path = entry if isinstance(entry, str) else (entry or {}).get("path")
        if isinstance(entry, str) and known.get(path):
            carried.append({"path": path, "uploaded_url": known[path]})
        else:
            carried.append(entry)
    return dict(fields, photos=carried)


def _push_rail(ctx: ToolContext, item: dict, changed: list) -> dict:
    """Push the change to the carousell.ai listing, answering how it went.

    No pacing reserve: a rail call is one API call on our own marketplace, not activity a buyer or
    the marketplace sees on the seller's account — the cross-link push takes none for the same
    reason. A refusal is reported in the rail's own words and never swallowed.
    """
    listing_id = listing_id_from_url(item["listing_urls"].get(_RAIL))
    if not listing_id:
        return {"status": "failed", "reason": "the recorded carousell.ai URL names no listing"}
    args: dict = {}
    if "title" in changed:
        args["title"] = item["title"]
    if "description" in changed:
        args["description"] = item["description"] or ""
    if "list_price" in changed:
        args["price_cents"] = to_price_cents(item["list_price"])
    try:
        rail = _rail(ctx)
        if "photos" in changed:
            args["media"] = _uploaded_media(ctx, item["id"])
        rail.update_listing(listing_id, **args)
    except ToolError as exc:
        return {"status": "failed", "reason": str(exc)}
    except RailError as exc:
        return {"status": "failed", "reason": str(exc)}
    return {"status": "updated"}


def _uploaded_media(ctx: ToolContext, item_id: str) -> dict:
    """The item's whole photo set as the rail wants it, uploading whatever is new first.

    The rail replaces its photo set wholesale, so a partial set would ship the wrong cover. The
    upload tool is all-or-nothing already; this asks it rather than repeating the bracket.
    """
    photos._upload_photos(ctx, {"item_id": item_id})
    item = ctx.store.get_item(item_id) or {}
    return {
        "urls": [
            {"url": photo["uploaded_url"], "type": _MEDIA_TYPE_IMAGE}
            for photo in item.get("photos") or []
            if photo.get("uploaded_url")
        ]
    }


def _queue_browser(ctx: ToolContext, item_id: str, market: str, url: str, changed: list) -> dict:
    """Owe a browser marketplace this edit, or say plainly why it will not happen by itself.

    Two answers are not "queued", and both carry the listing URL so the seller can change it by
    hand: a marketplace they have disconnected (acting on an account they switched off is exactly
    what disconnecting prevents), and one nothing here knows how to edit yet.
    """
    if market not in settings.connected_markets(ctx.store):
        return {"status": "not_connected", "url": url}
    if not (editor.can_edit_fields(market, changed) or marketplaces.edit_flow(market)):
        return {"status": "manual", "url": url}
    try:
        revision_id = ctx.store.queue_listing_revision(item_id, market, changed)
    except StoreError as exc:
        raise ToolError(str(exc)) from exc
    ctx.bus.publish("revise.queued", {"item_id": item_id, "market": market, "changed": changed})
    return {"status": "queued", "revision_id": revision_id}


def _record_listing_revision(ctx: ToolContext, params: dict) -> dict:
    """How an edit pass says how its edit went.

    The daemon cannot see a model-driven edit land, so this is how the outcome comes back — and the
    revise lane reports it to the seller from the row this writes. A pass may settle only the
    revision it was spawned for: the id alone is not enough, it must be attached to this pass, so a
    confused pass cannot close somebody else's edit as done.
    """
    revision = ctx.store.get_listing_revision(params["revision_id"])
    if revision is None or revision["status"] != "running":
        raise ToolError("no edit in progress with that id")
    if ctx.session.pass_id and revision.get("pass_id") != ctx.session.pass_id:
        raise ToolError("that edit belongs to another pass")
    outcome = params["outcome"]
    ctx.store.finish_listing_revision(
        revision["revision_id"],
        status=outcome,
        accepted=params.get("shown") if outcome == "done" else None,
        error=params.get("reason") or None,
    )
    ctx.bus.publish(
        "revise.settled",
        {"item_id": revision["item_id"], "market": revision["market"], "status": outcome},
    )
    return {"revision_id": revision["revision_id"], "status": outcome}


register(
    ToolSpec(
        name="record_listing_revision",
        description="Record how the listing edit you were given went, after reading the "
        "listing page back. outcome=done only when the page itself shows every changed field as "
        "the item now says; put what it shows in `shown`. Anything you could not confirm is "
        "failed, with the reason — the seller is told either way, so never record done on a hope.",
        input_schema={
            "type": "object",
            "properties": {
                "revision_id": {"type": "string"},
                "outcome": {"type": "string", "enum": ["done", "failed"]},
                "reason": {"type": "string"},
                "shown": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "price": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["revision_id", "outcome"],
            "additionalProperties": False,
        },
        handler=_record_listing_revision,
        tiers=frozenset({TIER_PASS_EDIT, TIER_ATTENDED}),
    )
)


register(
    ToolSpec(
        name="update_live_listing",
        description="Change an item's title, description, price or photos on the item AND on every "
        "marketplace it is listed on. Use this — never update_item — for anything already listed: "
        "update_item changes the record and nothing a buyer sees. `fields` takes any of title, "
        "description, list_price, photos (photos is the whole new set, as paths). Currency and "
        "condition cannot be changed on a live listing. Refuses a sold item, a paused agent, and a "
        "price change while a deal is in flight. The result has one status per marketplace: "
        "updated = carousell.ai shows it now; queued = a browser marketplace will be changed in "
        "the background and the seller gets a separate message when it lands or fails — say it "
        "has started, NEVER that it is done; not_connected / manual = it will not change by "
        "itself, so give the seller the url to change it there by hand; failed = say what failed, "
        "in the reason's words. floor_lowered = the seller's private floor moved down to the new "
        "price (say so without naming either number). offers_above_new_price counts standing "
        "offers higher than the new price.",
        input_schema={
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "fields": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "list_price": {"type": "number"},
                        "photos": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["item_id", "fields"],
            "additionalProperties": False,
        },
        handler=_update_live_listing,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED}),
    )
)
