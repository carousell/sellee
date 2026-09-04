"""The shipped marketplace registry — region→host resolution and display names.

A packaged data file (data/marketplaces.json), not user state: it backs the listing/search
recipes and the URL verifier. resolve_domain answers "which regional site of a marketplace does
this seller post on" (an SG seller lists to www.carousell.sg, not a global host) — and, by
answering None, "this marketplace has no site where this seller is", which is what makes a
marketplace ineligible for them. Pure and stdlib — reads the registry, mutates nothing.
"""

from __future__ import annotations

import json
from functools import lru_cache

from sellee.paths import PACKAGE_DATA_DIR

_REGISTRY_PATH = PACKAGE_DATA_DIR / "marketplaces.json"
SCAM_REGISTRY_PATH = PACKAGE_DATA_DIR / "scam_registry.json"

_ANY = "*"

# The rail: the marketplace every listing goes on, whatever else the seller enables.
RAIL = "carousell-ai"


@lru_cache(maxsize=1)
def _registry() -> dict:
    return json.loads(_REGISTRY_PATH.read_text())


def all_marketplaces() -> list[dict]:
    """Every registry entry, in file order."""
    return list(_registry().get("marketplaces", []))


def get_marketplace(market: str) -> dict | None:
    """The registry entry for a market id, or None if absent."""
    for entry in _registry().get("marketplaces", []):
        if entry.get("id") == market:
            return entry
    return None


def display_name(market: str) -> str:
    """The human name for a market id, or the id itself (fail-open) for an unknown market."""
    entry = get_marketplace(market)
    return (entry or {}).get("display_name") or market


def connector(market: str) -> dict:
    """How the agent reaches a market: `{type: mcp|browser, auth: …}`. Empty for an unknown one."""
    return (get_marketplace(market) or {}).get("connector") or {}


def connector_type(market: str) -> str:
    """`mcp` for a market with a first-party API (the rail), `browser` for one the agent drives in
    Chrome. The publish path branches on this, so an unknown market resolves to "" and matches
    neither rather than defaulting into one."""
    return str(connector(market).get("type") or "")


def urls(market: str) -> dict:
    """The market's recorded page templates (`inbox`, `thread`, `my_listings`, …)."""
    return (get_marketplace(market) or {}).get("urls") or {}


def media_hosts(market: str) -> list:
    """The hosts a market serves its listing photographs from.

    A capability bound: the photo fetch refuses a URL whose host is not in here, so a listing page
    that hands back a link somewhere else is not downloaded. A market with none listed cannot have
    photos brought across.
    """
    return list((get_marketplace(market) or {}).get("media_hosts") or [])


def media_host_suffixes(market: str) -> list:
    """Domains a market's listing photographs may sit under, matched on a dot boundary.

    For image hosts generated per request, which `media_hosts` cannot enumerate. An entry with
    neither this nor `media_hosts` fetches nothing.
    """
    return list((get_marketplace(market) or {}).get("media_host_suffixes") or [])


def listing_flow(market: str) -> str:
    """The skill holding this market's publish recipe, or "" when it has none."""
    return str((get_marketplace(market) or {}).get("listing_flow") or "")


def browser_markets() -> list[str]:
    """Active markets the agent drives through Chrome, in registry order."""
    return [
        entry["id"]
        for entry in all_marketplaces()
        if (entry.get("connector") or {}).get("type") == "browser"
        and entry.get("status") == "active"
    ]


def market_url(market: str, key: str, region: str | None = None, **fields) -> str | None:
    """A page URL for a market, composed from the registry and nowhere else.

    Every navigation target the agent uses comes from here, a stored listing URL, or a link read off
    a live page — never from a guess. A composed inbox or chat URL that was remembered rather than
    recorded is how a pass ends up touring a dead page, so an unrecorded template resolves to None
    and the caller reports that instead of inventing one.
    """
    path = urls(market).get(key)
    host = resolve_domain(market, region)
    if not path or not host:
        return None
    try:
        path = path.format(**fields) if fields else path
    except (KeyError, IndexError):
        return None
    return f"https://{host}{path}"


def supported_regions() -> list:
    """Where the agent can be used at all — the regions the rail serves, in registry order.

    Every listing goes on the rail, so a seller the rail has no site for cannot be sold for,
    whatever browser marketplaces might exist around them. Derived from the registry rather than
    listed a second time here: the day the rail opens a country, this answers with it and no code
    changes.
    """
    return sorted((get_marketplace(RAIL) or {}).get("domains") or {})


def market_home(market: str, region: str | None = None) -> str | None:
    """The marketplace's front page for a seller in this region, or None when it has no site there.

    The one page every marketplace has and no registry needs a template for. It is where a
    sign-in starts: logged out it shows the login screen, logged in it shows the signed-in header
    the login probe reads.
    """
    host = resolve_domain(market, region)
    return f"https://{host}/" if host else None


def has_regional_site(market: str, region: str | None = None) -> bool:
    """Whether this marketplace names a site for this region, rather than a catch-all.

    `resolve_domain` deliberately collapses the two (either way there is a site to drive), so the
    connect offer asks for the difference to put the home marketplace first.
    """
    entry = get_marketplace(market)
    if entry is None:
        return False
    return bool((entry.get("domains") or {}).get(region))


def resolve_domain(market: str, region: str | None = None) -> str | None:
    """The marketplace's site for a seller in this region, or None when it has none.

    An entry that enumerates its regional sites is authoritative: a region absent from the map is a
    region the marketplace does not serve, which is how "Carousell runs seven regional sites and no
    US one" is stated. Only an entry with no map at all falls back to its listing host, and only
    when that is a real host — a bare suffix like "carousell." is the verifier's host *pattern*, and
    handing it out as a site produces URLs that cannot resolve and region checks that compare
    against nonsense.
    """
    entry = get_marketplace(market)
    if entry is None:
        return None
    domains = entry.get("domains") or {}
    if domains:
        return domains.get(region) or domains.get(_ANY)
    host = (entry.get("listing_url") or {}).get("host") or None
    return host if host and not host.endswith(".") else None
