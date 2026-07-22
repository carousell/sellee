"""verify_listing_url — the deterministic guard against fabricated listing links.

For a browser marketplace the check is pure: host + path (+ region-specific host when a region is
given) against the shipped registry pattern, via the shared host matcher. carousell-ai is the
exception — its recorded link is a first-party page on the live web app, so it goes through the
rail client's fail-closed live 200 check. A hallucinated, wrong-host, wrong-region, or dead link
fails closed either way. The same matcher is also enforced internally at every listing_urls write
seam; this tool exists for a pre-message check.
"""

from __future__ import annotations

from .. import marketplaces
from ..engines import hosts
from ..rail.client import RailError, RailUnprovisioned
from .registry import (
    TIER_ATTENDED,
    TIER_PASS_REPLY,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)

_CAROUSELL_AI = "carousell-ai"


def _verify_listing_url(ctx: ToolContext, params: dict) -> dict:
    market = params["market"]
    url = params["url"]
    region = params.get("region")

    if market == _CAROUSELL_AI:
        return _verify_carousell_ai_live(ctx, market, url)

    entry = marketplaces.get_marketplace(market)
    pattern = (entry or {}).get("listing_url") or {}
    host_pattern = pattern.get("host")
    path_pattern = pattern.get("path")
    if not host_pattern or not path_pattern:
        return {
            "ok": False,
            "market": market,
            "reason": f"no listing pattern for market {market!r}",
        }
    region_host = marketplaces.resolve_domain(market, region) if region else None
    ok, reason = hosts.verify_listing_pattern(url, host_pattern, path_pattern, region_host)
    if ok:
        return {"ok": True, "market": market, "url": url.strip()}
    return {"ok": False, "market": market, "reason": reason}


def _verify_carousell_ai_live(ctx: ToolContext, market: str, url: str) -> dict:
    if ctx.rail_factory is None:
        raise ToolError("the carousell.ai rail is not available in this session")
    try:
        rail = ctx.rail_factory()
    except RailUnprovisioned as exc:
        raise ToolError(
            "carousell.ai is not provisioned — run `selly-agent provision carousell-ai`"
        ) from exc
    try:
        rail.verify_listing_url(url)
    except RailError as exc:
        # A wrong base, non-200, or unreachable host all fail closed — never a fabricated link.
        return {"ok": False, "market": market, "reason": str(exc)}
    return {"ok": True, "market": market, "url": (url or "").strip()}


register(
    ToolSpec(
        name="verify_listing_url",
        description="Verify a listing URL against the marketplace's registered host/path (and "
        "region when given); carousell-ai does a live 200 check. Fails closed on a fabricated URL.",
        input_schema={
            "type": "object",
            "properties": {
                "market": {"type": "string"},
                "url": {"type": "string"},
                "region": {"type": "string"},
            },
            "required": ["market", "url"],
            "additionalProperties": False,
        },
        handler=_verify_listing_url,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_REPLY}),
    )
)
