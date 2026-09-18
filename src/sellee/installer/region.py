"""Guessing where the seller sells, so setup can confirm rather than interrogate.

Nothing here decides where a seller may sell: any country is accepted, and a zone the table does
not name produces no guess at all, so setup asks rather than proposing a wrong default.
"""

from __future__ import annotations

import os

# What a region likely prices in, for the confirm line only. bazaar decides the real currency of
# a listing, so a country missing here costs nothing.
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


def region_for_zone(zone: str):
    """The region a timezone implies, or None when it implies nothing."""
    if not zone:
        return None
    found = _ZONE_REGIONS.get(zone)
    if found is None:
        for prefix, region in _ZONE_PREFIX_REGIONS:
            if zone.startswith(prefix):
                found = region
                break
    return found


def zones_for(region: str) -> list:
    """The zones this region is known by, in table order — the most populous first."""
    return [zone for zone, code in _ZONE_REGIONS.items() if code == region]


def default_zone(region: str, zone: str | None = None) -> str:
    """The timezone to propose once the country is known.

    The machine's zone first — the clock check reads the stored zone as a claim about this
    machine, not about where they sell. Otherwise the country's zone, but only when the country
    has exactly one; several means the question must be asked.
    """
    zone = system_timezone() if zone is None else zone
    if zone:
        return zone
    zones = zones_for(region)
    return zones[0] if len(zones) == 1 else ""


def system_timezone() -> str:
    """The machine's IANA zone name, or "" when it cannot be read.

    TZ wins, because POSIX says it overrides the machine's default and in a container it is the
    only signal there is: setting it moves the clock but leaves /etc/localtime pointing at UTC,
    so the two disagree and the file is the one that is wrong.

    Otherwise where /etc/localtime points, rather than `time.tzname`, which gives an abbreviation
    ("+08") that names no zone and cannot be stored or looked up. Either way the answer is checked
    against the zone database: a name that resolves to nothing is worse than none.
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

    The database directory is not always literally `zoneinfo` — macOS resolves /etc/localtime to
    /usr/share/zoneinfo.default — so any directory whose name starts with `zoneinfo` counts. Read
    from the right so an unrelated `zoneinfo` directory further up cannot claim the rest; the
    caller checks the result against the zone database anyway.
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


def zone_error(name: str) -> str:
    """Why this timezone cannot be stored, or "" when it can.

    The same rule `tools.seller.validate_basics` applies, so a typo is re-asked at the prompt
    rather than rejected at the write door. That door stays the authority, so this must never be
    stricter — hence a machine with no zone database vouches for nothing rather than rejecting
    every name.
    """
    import zoneinfo

    if not name:
        return "a timezone is needed"
    try:
        zoneinfo.ZoneInfo(name)
        return ""
    except zoneinfo.ZoneInfoNotFoundError:
        pass
    except (ValueError, OSError):
        return f"{name!r} is not a valid timezone name"
    try:
        zoneinfo.ZoneInfo("UTC")
    except Exception:  # noqa: BLE001 — no zone database here, so there is nothing to check against
        return ""
    return f"unknown timezone {name!r}"


def guess(zone: str | None = None):
    """A {region, timezone} proposal, or None when the machine gives no hint. No currency: what a
    listing is priced in comes back from bazaar, so proposing one would be recording a guess."""
    zone = system_timezone() if zone is None else zone
    region = region_for_zone(zone)
    return None if region is None else {"region": region, "timezone": zone}


def render(basics: dict) -> str:
    """How a proposal is put to the seller: `SG · SGD · Asia/Singapore`. An unrecorded currency
    is filled in from the table for this line only, and is dropped for a country not in it."""
    shown = basics if basics.get("currency") else {**basics, "currency": _likely_currency(basics)}
    return " · ".join(
        str(shown.get(key, "")) for key in ("region", "currency", "timezone") if shown.get(key)
    )


def _likely_currency(basics: dict) -> str:
    return CURRENCIES.get(basics.get("region", ""), "")
