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

import os

from sellee import marketplaces

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
    """The machine's IANA zone name, or "" when it cannot be read.

    TZ wins, because POSIX says it overrides the machine's default and in a container it is the
    only signal there is: setting it moves the clock but leaves /etc/localtime pointing at UTC,
    so the two disagree and the file is the one that is wrong.

    Otherwise where /etc/localtime points, rather than `time.tzname`, which gives an abbreviation
    ("+08") that names no zone and cannot be stored or looked up. Either way the answer is checked
    against the zone database before it is handed back: the whole point of this function is to
    produce a name that can be stored and looked up later, and a name that resolves to nothing is
    worse than admitting the machine said nothing.
    """
    named = os.environ.get("TZ", "").strip().lstrip(":")
    if named and _zone_exists(named):
        return named
    try:
        resolved = os.path.realpath("/etc/localtime")
    except OSError:
        return ""
    zone = _zone_from_path(resolved)
    return zone if _zone_exists(zone) else ""


def _zone_from_path(resolved: str) -> str:
    """The zone name inside a path to a compiled zone file, or "" when there is none.

    The database directory is not always called `zoneinfo`. macOS points /etc/localtime through
    /var/db/timezone/zoneinfo at /usr/share/zoneinfo.default, so looking for the literal
    "/zoneinfo/" matched nothing on every Mac — and a machine that reports no zone is a machine
    setup cannot propose a region for, so it asks instead, with an empty timezone field and only
    "e.g. Asia/Singapore" to go on. That is where "gmt8+" comes from.

    Read from the right, so a path with a directory of its own called `zoneinfo` further up
    cannot claim the rest of it. Matching the prefix rather than the exact names keeps the next
    variant of this from being another silent empty answer; the caller checks the result against
    the database anyway, so being liberal here costs nothing.
    """
    parts = resolved.split("/")
    for index in range(len(parts) - 2, -1, -1):
        if parts[index].startswith("zoneinfo"):
            return "/".join(parts[index + 1 :])
    return ""


def _zone_exists(name: str) -> bool:
    """TZ also takes POSIX forms — "<+08>-8", "UTC+8" — that name no zone and cannot be stored,
    so only a name the database has is worth proposing."""
    import zoneinfo

    try:
        zoneinfo.ZoneInfo(name)
    except Exception:  # noqa: BLE001 — malformed, unknown, or no database: none of them usable
        return False
    return True


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
