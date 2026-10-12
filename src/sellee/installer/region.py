"""Guessing where the seller sells, so setup can confirm rather than interrogate.

Nothing here decides where a seller may sell: any country is accepted, and a zone the table does
not name produces no guess at all, so setup asks rather than proposing a wrong default.
"""

from __future__ import annotations

import functools
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from sellee import countries

# Timezones that identify a region unambiguously. Zones are listed rather than matched by an
# `America/*`-style prefix: that prefix also covers Toronto, Mexico City and São Paulo, and
# answering "US" for those would be wrong in a way the seller has no reason to double-check.
# Canada is enumerated for the same reason rather than sharing the prefix.
#
# The countries here are the ones a price can be quoted in something other than USD, so a guess
# and the currency shown beside it cover the same ground. A country absent here is not a country
# the agent refuses — it is one the machine cannot vouch for, so setup asks instead.
_ZONE_REGIONS = {
    "Asia/Singapore": "SG",
    "Asia/Ho_Chi_Minh": "VN",
    "Asia/Kuala_Lumpur": "MY",
    "Asia/Kuching": "MY",
    "Asia/Jakarta": "ID",
    "Asia/Makassar": "ID",
    "Asia/Jayapura": "ID",
    "Asia/Bangkok": "TH",
    "Asia/Manila": "PH",
    "Asia/Taipei": "TW",
    "Asia/Hong_Kong": "HK",
    "Asia/Tokyo": "JP",
    "Asia/Seoul": "KR",
    "Asia/Kolkata": "IN",
    "Asia/Karachi": "PK",
    "Asia/Brunei": "BN",
    "Europe/London": "GB",
    "Europe/Berlin": "DE",
    "Europe/Amsterdam": "NL",
    "Europe/Brussels": "BE",
    "Europe/Rome": "IT",
    "Australia/Sydney": "AU",
    "Pacific/Auckland": "NZ",
    "America/Toronto": "CA",
    "America/Vancouver": "CA",
    "America/Edmonton": "CA",
    "America/Winnipeg": "CA",
    "America/Halifax": "CA",
    # Setup lists a country's zones in this order, so the most populous lead.
    "America/New_York": "US",
    "America/Chicago": "US",
    "America/Denver": "US",
    "America/Los_Angeles": "US",
    "America/Phoenix": "US",
    "America/Anchorage": "US",
    "Pacific/Honolulu": "US",
    "America/Detroit": "US",
    "America/Boise": "US",
    "America/Juneau": "US",
    "America/Sitka": "US",
    "America/Nome": "US",
}

# The legacy `US/Eastern`-style aliases, still what some machines report. `Australia/` is a
# prefix rather than a list because every zone under it is in Australia — the objection to
# `America/` does not apply.
_ZONE_PREFIX_REGIONS = (
    ("US/", "US"),
    ("America/Indiana/", "US"),
    ("America/Kentucky/", "US"),
    ("Australia/", "AU"),
)


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


# Where macOS and Debian keep their zone files, and `zone.tab` beside them: the OS's zones by
# country, with labels. Without it setup uses the table above, then asks for a city.
_USUAL_ZONE_DIR = "/usr/share/zoneinfo"


def zone_tab_path() -> str:
    """The `zone.tab` of the first zone directory the interpreter searches that has one."""
    import zoneinfo

    for directory in (*zoneinfo.TZPATH, _USUAL_ZONE_DIR):
        path = os.path.join(directory, "zone.tab")
        if os.path.isfile(path):
            return path
    return os.path.join(_USUAL_ZONE_DIR, "zone.tab")


def place_zones(region: str, zone_tab: str | None = None) -> list:
    """(label, zone) pairs to offer for this region: the table above's zones in its order, then any
    others `zone.tab` lists for it. Each is labelled by `zone.tab`'s description of where it
    applies and the zone's city ("Eastern — New York"), so a seller in a city no zone is named
    after can still find theirs."""
    described = {zone: label for code, zone, label in _read_zone_tab(zone_tab) if code == region}
    table = zones_for(region)
    zones = table + [zone for zone in described if zone not in table]
    return [
        (_place_label(zone, described.get(zone, "")), zone)
        for zone in zones
        if not zone_error(zone)
    ]


# How `zone.tab` marks a country's main zone: "Eastern (most areas)", "most of Germany".
_MAIN_AREA = re.compile(r"^most of |\s*\(most areas\)")


def _place_label(zone: str, description: str) -> str:
    """The description and the city, or whichever alone says it all: "Hawaii — Honolulu",
    "Canary Islands", "Paris"."""
    area = _MAIN_AREA.sub("", description).strip()
    city = city_label(zone)
    if not area:
        return city
    return area if _spelling(city) in _spelling(area) else f"{area} — {city}"


def zones_for_city(text: str, zone_tab: str | None = None) -> list:
    """The zones whose city is `text`, ignoring accents, case and punctuation. Names that keep the
    same time are one answer, not a choice, so only the likeliest-canonical of them is kept."""
    wanted = _city_key(text)
    if not wanted:
        return []
    listed = {zone for _, zone, _ in _read_zone_tab(zone_tab)}
    found = sorted(
        (zone for zone in _zones_by_city().get(wanted, ()) if not zone_error(zone)),
        key=lambda zone: _canonical_first(zone, listed, text.strip()),
    )
    return _one_per_clock(found)


# The areas canonical zone names sit under. A name outside them ("Singapore", "US/Pacific") is a
# link kept for old systems.
_ZONE_AREAS = frozenset(
    "Africa America Antarctica Arctic Asia Atlantic Australia Europe Indian Pacific".split()
)


def _canonical_first(zone: str, listed: set, typed: str) -> tuple:
    """Sorts first the name to offer: one `zone.tab` lists, else one under a continent or ocean
    (Asia/Singapore over Singapore), else exactly what was typed (UTC), else the deepest, because
    a city moved under its state left a shorter link behind (America/Indianapolis)."""
    in_area = zone.split("/", 1)[0] in _ZONE_AREAS
    return (zone not in listed, not in_area, zone != typed, -zone.count("/"), zone)


# Offsets are compared weekly from 1970, where the database's history is complete, for 70 years.
_CLOCK_SINCE = datetime(1970, 1, 1, tzinfo=timezone.utc)
_CLOCK_WEEKS = 52 * 70


def _one_per_clock(zones: list) -> list:
    """`zones` less any that keep the same time as one before it."""
    if len(zones) < 2:
        return zones
    clocks = [_clock(zone) for zone in zones]
    return [zone for index, zone in enumerate(zones) if clocks[index] not in clocks[:index]]


def _clock(zone: str) -> tuple:
    import zoneinfo

    tz = zoneinfo.ZoneInfo(zone)
    return tuple(
        (_CLOCK_SINCE + timedelta(weeks=week)).astimezone(tz).utcoffset()
        for week in range(_CLOCK_WEEKS)
    )


@functools.lru_cache(maxsize=1)
def _zones_by_city() -> dict:
    """Every zone the database has, by its city's spelling. Built once: listing the database opens
    every file in it."""
    import zoneinfo

    index: dict = {}
    for zone in sorted(zoneinfo.available_timezones()):
        index.setdefault(_city_key(city_label(zone)), []).append(zone)
    return {key: tuple(zones) for key, zones in index.items()}


def city_label(zone: str) -> str:
    """A zone's city spelled as a place: "Los Angeles" for America/Los_Angeles."""
    return zone.rsplit("/", 1)[-1].replace("_", " ")


def _city_key(text: str) -> str:
    """How a typed city is compared with a zone's: the part before any comma, so "Austin, TX" is
    Austin, spelled as `_spelling` reduces it."""
    return _spelling(text.split(",", 1)[0])


# Dropped from a spelling: everything but letters and digits, except the sign of an offset, which
# is all that tells GMT+8 from GMT-8.
_NOT_SPELLING = re.compile(r"[^0-9a-z+-]|[+-](?![0-9])")


def _spelling(text: str) -> str:
    """`text` without accents, case, spaces or punctuation: "St. John's" and "St_Johns" agree."""
    folded = "".join(unicodedata.normalize("NFKD", text).casefold().split())
    return _NOT_SPELLING.sub("", folded)


def _read_zone_tab(path: str | None) -> list:
    """(country, zone, label) per line of `zone.tab`, or none when it cannot be read."""
    try:
        with open(path or zone_tab_path(), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        fields = line.split("\t")
        if line.startswith("#") or len(fields) < 3:
            continue
        rows.append((fields[0], fields[2], fields[3].strip() if len(fields) > 3 else ""))
    return rows


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
    listing is priced in comes from the backend, so proposing one would be recording a guess."""
    zone = system_timezone() if zone is None else zone
    region = region_for_zone(zone)
    return None if region is None else {"region": region, "timezone": zone}


def render(basics: dict) -> str:
    """How a proposal is put to the seller: `SG — Singapore · SGD · Asia/Singapore`. The country
    is spelled out because "SA" and "SG" look equally right until one of them is named."""
    parts = [countries.label(basics["region"])] if basics.get("region") else []
    parts += [str(basics[key]) for key in ("currency", "timezone") if basics.get(key)]
    return " · ".join(parts)
