"""The checkout-at-close, and the one wall in front of it.

`carousell_ai_create_checkout_link` composes legacy's three-step choreography (precheck → LLM MCP
call → record) into one tool: sale-id idempotency → floor gate → resolve listing_id → mint over the
rail (outside any store transaction) → fail-closed URL-base validation → record. Idempotency runs
first so an already-issued link is returned as-is even if the floor moved since — the floor gate
only guards genuinely new checkout attempts. The floor never crosses the boundary: a below-floor
close returns a structured below_floor error carrying no value, a floorless below-list close returns
no_floor (ask the seller), and an unpublished item returns not_published naming the publish tool —
the legacy self-heal (inline create-then-checkout) is deliberately not ported.

`carousell_ai_create_signin_link` is the wall's way out: the rail refuses to mint checkout links
for an account that has never been signed into, and the refusal maps to guidance naming this tool.
The sign-in URL grants ownership of the seller's account, so the tool is seller-channel only —
invisible to the buyer-facing reply tier, whose refusal wording says escalate instead and never
names a tool it cannot call.
"""

from __future__ import annotations

import hashlib

from sellee.money import to_price_cents
from sellee.rail.client import (
    RailError,
    RailToolError,
    RailUnprovisioned,
    listing_id_from_url,
)
from sellee.store import StoreError
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_REPLY,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)

_MARKET = "carousell-ai"

# The rail deliberately ships a bare refusal for a guest account — no error code to match on (a
# sign-in URL must never ride a buyer-facing error path). This clause is the stable part of that
# copy; if it drifts, the mapping below stops firing and the raw rail text surfaces as before —
# degraded (it still says the seller must sign in), never wrong.
_GUEST_GATE_CLAUSE = "belongs to a guest account"
_ALREADY_SIGNED_IN_CLAUSE = "already a seller"

_GUEST_GATE_SELLER_GUIDANCE = (
    "the seller hasn't done their one-time carousell.ai sign-in yet, so checkout links are "
    "refused. Mint a sign-in link with carousell_ai_create_signin_link and send it to the seller "
    "with a one-line why; once they say they've signed in, call this tool again"
)
_GUEST_GATE_REPLY_GUIDANCE = (
    "the seller hasn't completed a one-time carousell.ai sign-in, so no checkout link can be "
    "minted yet. Post the buyer a neutral holding line — never mention the seller or their "
    "account — then escalate to the seller asking them to sign in; the seller's own channel "
    "handles the link"
)


def _sale_id(item_id: str, thread_id: str, price) -> str:
    seed = f"{item_id}|{thread_id}|{price}".encode()
    return hashlib.sha256(seed).hexdigest()[:12]  # an id, not a security hash


def _listing_id(item: dict) -> str:
    return listing_id_from_url((item.get("listing_urls") or {}).get(_MARKET))


def _checkout_base(ctx: ToolContext) -> str:
    # the web origin, not the API origin: real checkout pages are served on www.carousell.ai
    return ctx.config.carousell_ai_web_base_url.rstrip("/") + "/checkout"


def _signin_base(ctx: ToolContext) -> str:
    return ctx.config.carousell_ai_web_base_url.rstrip("/") + "/signin"


def _rail(ctx: ToolContext):
    if ctx.rail_factory is None:
        raise ToolError("the carousell.ai rail is not available in this session")
    try:
        return ctx.rail_factory()
    except RailUnprovisioned as exc:
        raise ToolError(
            "carousell.ai is not provisioned — run `sellee provision carousell-ai`"
        ) from exc


def _guest_gate_guidance(ctx: ToolContext) -> str:
    """The remedy, worded per tier: naming the sign-in tool to a tier that cannot see it would
    send the model at an unknown tool, so the reply tier gets the escalate route instead."""
    if ctx.session.tier == TIER_PASS_REPLY:
        return _GUEST_GATE_REPLY_GUIDANCE
    return _GUEST_GATE_SELLER_GUIDANCE


def _create_checkout_link(ctx: ToolContext, params: dict) -> dict:
    item_id = params["item_id"]
    thread_id = params["thread_id"]
    price = params["agreed_price"]

    item = ctx.store.get_item(item_id)
    if item is None:
        raise ToolError(f"no item with id {item_id!r}")

    # Idempotency first: an already-issued link for this exact (item, thread, price) returns
    # as-is — the floor can have moved since the deal closed, and nothing about a recorded sale
    # needs re-validating against it.
    sale_id = _sale_id(item_id, thread_id, price)
    existing = ctx.store.get_checkout(sale_id)
    if existing:
        return {"checkout_url": existing["checkout_url"], "already_issued": True}

    # floor gate — the store returns only a status, never the floor value
    try:
        gate = ctx.store.checkout_floor_gate(item_id, price)
    except StoreError as exc:
        raise ToolError(str(exc)) from exc
    if gate["status"] == "below_floor":
        raise ToolError("agreed price is not acceptable for this item")
    if gate["status"] == "no_floor":
        raise ToolError(
            "no floor is set and the price is below list — ask the seller for their floor first"
        )

    listing_id = _listing_id(item)
    if not listing_id:
        raise ToolError(
            "item is not published on carousell.ai — publish it first with "
            "carousell_ai_publish_listing, then create the checkout link"
        )

    rail = _rail(ctx)

    try:
        price_cents = to_price_cents(price)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc

    try:
        minted = rail.create_checkout({"listing_id": listing_id, "agreed_price_cents": price_cents})
    except RailToolError as exc:
        if _GUEST_GATE_CLAUSE in str(exc):
            raise ToolError(_guest_gate_guidance(ctx)) from exc
        raise ToolError(str(exc)) from exc
    except RailError as exc:
        raise ToolError(str(exc)) from exc

    url = (minted.get("checkout_url") or "").strip()
    base = _checkout_base(ctx)
    if not url.startswith(base + "/"):
        # fail closed: a minted link must sit under the config-derived checkout base
        raise ToolError("checkout link did not come from the expected carousell.ai checkout base")

    recorded = ctx.store.record_checkout(
        sale_id=sale_id,
        item_id=item_id,
        thread_id=thread_id,
        checkout_url=url,
        price=price,
        currency=gate["currency"],
    )
    return {"checkout_url": recorded["checkout_url"]}


register(
    ToolSpec(
        name="carousell_ai_create_checkout_link",
        description="Mint (or return the already-issued) carousell.ai checkout link for a "
        "finalised deal. Floor-gated and idempotent; an unpublished item returns not_published.",
        input_schema={
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "thread_id": {"type": "string"},
                "agreed_price": {"type": "number"},
            },
            "required": ["item_id", "thread_id", "agreed_price"],
            "additionalProperties": False,
        },
        handler=_create_checkout_link,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_REPLY, TIER_PASS_CHANNEL}),
    )
)


def _create_signin_link(ctx: ToolContext, params: dict) -> dict:
    rail = _rail(ctx)
    try:
        minted = rail.create_promotion_url()
    except RailToolError as exc:
        if _ALREADY_SIGNED_IN_CLAUSE in str(exc):
            # a result, not an error — an error here would read as failure and invite re-mint loops
            return {
                "already_signed_in": True,
                "note": "the seller has already signed in — create the checkout link now",
            }
        raise ToolError(str(exc)) from exc
    except RailError as exc:
        raise ToolError(str(exc)) from exc

    url = (minted.get("promotion_url") or "").strip()
    base = _signin_base(ctx)
    # fail closed — this URL hands over the seller's account. Exact base or a real `?`/`/`
    # boundary; a bare prefix match would also accept a same-origin `/signinfoo`.
    if url != base and not url.startswith((base + "?", base + "/")):
        raise ToolError("sign-in link did not come from the expected carousell.ai sign-in base")
    return {"signin_url": url}


register(
    ToolSpec(
        name="carousell_ai_create_signin_link",
        description="Mint a fresh carousell.ai sign-in link for the seller's one-time Google "
        "sign-in — required before checkout links can be created. Send it to the seller on their "
        "own channel ONLY (never to a buyer, never into a listing or note). The link expires in "
        "about 15 minutes; mint a fresh one any time. Returns already_signed_in when the account "
        "no longer needs it.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_create_signin_link,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_CHANNEL}),
    )
)
