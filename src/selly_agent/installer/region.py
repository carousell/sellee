"""Guessing where the seller sells, so setup can confirm rather than interrogate.

Region is the first thing the agent needs and the least interesting thing to ask for: it decides
which regional marketplace sites exist for this seller and which currency their prices are in.
The machine already knows its timezone, so setup proposes an answer and asks for a yes.

The table covers only the countries the rail serves, because those are the only ones the product
works in — a guess outside them would hand someone a confident answer that setup then has to
refuse. Anything unmapped produces no guess at all, and setup asks instead: one extra question is
cheaper than a wrong default, and far cheaper than a seller configured for a country where
nothing they list can go anywhere.
"""

from __future__ import annotations

import tzlocal

from selly_agent import marketplaces

# What a region prices in. Only the regions the rail serves are here; `supported()` is the
# authority on which those are, and this must not get ahead of it.
CURRENCIES = {
    "SG": "SGD",
    "US": "USD",
}

# Timezones that identify a region unambiguously. US zones are listed rather than matched by an
# `America/*` prefix: that prefix also covers Toronto, Mexico City and São Paulo, and answering
# "US" for those would be wrong in a way the seller has no reason to double-check.
_ZONE_REGIONS = {
    "Asia/Singapore": "SG",
    "America/New_York": "US",
    "America/Detroit": "US",
    "America/Chicago": "US",
    "America/Denver": "US",
    "America/Phoenix": "US",
    "America/Los_Angeles": "US",
    "America/Anchorage": "US",
    "America/Boise": "US",
    "America/Juneau": "US",
    "America/Nome": "US",
    "America/Sitka": "US",
    "Pacific/Honolulu": "US",
}

# The legacy `US/Eastern`-style aliases, still what some machines report.
_ZONE_PREFIX_REGIONS = (("US/", "US"), ("America/Indiana/", "US"), ("America/Kentucky/", "US"))


def supported() -> list:
    """The regions setup will accept, straight from the registry."""
    return marketplaces.supported_regions()


def region_for_zone(zone: str):
    """The region a timezone implies, or None when it implies nothing we support."""
    if not zone:
        return None
    found = _ZONE_REGIONS.get(zone)
    if found is None:
        for prefix, region in _ZONE_PREFIX_REGIONS:
            if zone.startswith(prefix):
                found = region
                break
    return found if found in supported() else None


def system_timezone() -> str:
    """The machine's IANA zone name, or "" when it cannot be determined.

    An IANA name specifically, not `time.tzname`, which gives an abbreviation ("+08") that names no
    zone and cannot be stored or looked up. Where that name comes from differs per platform —
    /etc/localtime on POSIX, a registry key that has to be mapped on Windows — which is why it is
    asked of tzlocal rather than read here. Returning "" is a supported answer: the installer then
    asks the seller instead of guessing.
    """
    try:
        return tzlocal.get_localzone_name() or ""
    except Exception:  # noqa: BLE001 — any failure to read the machine's zone means "ask instead"
        return ""


def guess(zone: str | None = None):
    """A complete {region, currency, timezone} proposal, or None when the machine gives no hint."""
    zone = system_timezone() if zone is None else zone
    region = region_for_zone(zone)
    if region is None:
        return None
    currency = CURRENCIES.get(region)
    if currency is None:
        return None
    return {"region": region, "currency": currency, "timezone": zone}


def render(basics: dict) -> str:
    """How a proposal is put to the seller: `SG · SGD · Asia/Singapore`."""
    return " · ".join(
        str(basics.get(key, "")) for key in ("region", "currency", "timezone") if basics.get(key)
    )
