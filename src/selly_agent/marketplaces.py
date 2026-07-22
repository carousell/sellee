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
