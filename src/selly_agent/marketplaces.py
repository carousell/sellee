"""The shipped marketplace registry — region→host resolution and display names.

A packaged data file (data/marketplaces.json), not user state: it backs the listing/search
recipes and the URL verifier. resolve_domain answers "which regional site of a marketplace does
this seller post on" (an SG seller lists to www.carousell.sg, not a global host) with a
first-match rule: an exact regional host, then the marketplace's "*" default, then the
listing_url host suffix for entries with no domains map. Pure and stdlib — reads the registry,
mutates nothing.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

# Package data lives beside the code (like the migration SQL), read via a package-relative path
# — never through paths.py, which is the home/XDG authority for user state, not shipped assets.
PACKAGE_DATA_DIR = Path(__file__).resolve().parent / "data"
_REGISTRY_PATH = PACKAGE_DATA_DIR / "marketplaces.json"
SCAM_REGISTRY_PATH = PACKAGE_DATA_DIR / "scam_registry.json"

_ANY = "*"


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
    if not path or not host or host.endswith("."):
        return None
    try:
        path = path.format(**fields) if fields else path
    except (KeyError, IndexError):
        return None
    return f"https://{host}{path}"


def resolve_domain(market: str, region: str | None = None) -> str | None:
    """The region-specific host for a market, or None if unresolvable. First match wins:
    the exact regional host, then the "*" default, then the listing_url host suffix."""
    entry = get_marketplace(market)
    if entry is None:
        return None
    domains = entry.get("domains") or {}
    if region and region in domains:
        return domains[region]
    if _ANY in domains:
        return domains[_ANY]
    return (entry.get("listing_url") or {}).get("host") or None
