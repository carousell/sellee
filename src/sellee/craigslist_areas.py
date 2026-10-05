"""Craigslist's areas, and the two gates that decide which of them we serve.

Craigslist's unit of geography is one of 707 *areas*, not a country, and each carries two
identifiers that are **not interchangeable**:

- `hostname` — what a seller sees in their own URL, `www.craigslist.org/area/sfbay`;
- `abbreviation` — what the posting flow requires, `post.craigslist.org/c/sfo`.

Feeding the hostname to the posting flow does not error: it answers HTTP 200 and lands on a generic
"choose area" picker, from which a listing goes wherever that picker decides. A wrong area cannot be
corrected after publishing, so resolution here always answers the **abbreviation**, and a hostname
is only ever an input.

Two allowlists gate which areas we serve, because they answer different questions and will change at
different times:

- `SERVED_STATES` is the **legal envelope** — the US states we may facilitate transactions in.
- `CRAIGSLIST_AREAS` is the **go-to-market rollout** — the areas we have actually launched.

`CRAIGSLIST_AREAS` is asserted to sit inside `SERVED_STATES` at import. That assertion is what makes
adding a state *safe* rather than merely easy: launching an area whose state we are not cleared for
fails at import until the state is added too.

The state itself comes from Craigslist's own data rather than a table we maintain. Note the word:
Craigslist's `Region` field is a **US state code**, while `region` everywhere else in sellee is an
**ISO country code** — a "CA" means California in one and Canada in the other. This module calls it
`state` for that reason, and never speaks about countries except to compare one.

Pure and stdlib: reads the vendored snapshot, mutates nothing, opens no socket. The snapshot is
vendored rather than fetched because `reference.craigslist.org` publishes no licence or stability
guarantee, and because fetching it would put a socket in a module that does not need one.
"""

from __future__ import annotations

import difflib
import json
import re
from functools import lru_cache

from sellee.paths import PACKAGE_DATA_DIR

_SNAPSHOT_PATH = PACKAGE_DATA_DIR / "craigslist_areas.json"

# The states we may facilitate transactions in. Add a state here to open it.
SERVED_STATES = frozenset({"CA"})

# The areas we have launched, as Craigslist abbreviations. Add an abbreviation here to roll out —
# its state must already be in SERVED_STATES, which `_check_rollout_is_legal` enforces at import.
CRAIGSLIST_AREAS = frozenset({"sfo"})

# How many close matches an unknown area names back. A typo should not read as "craigslist does not
# go there", but a list of twenty guesses is not a suggestion either.
_SUGGESTIONS = 3


class UnknownArea(ValueError):
    """No Craigslist area answers to this. Caller-facing: the message names close matches, because
    the common cause is a typo rather than a place Craigslist does not cover."""


class AreaNotAvailable(ValueError):
    """A real Craigslist area we do not serve. `reason` separates the two cases, which a seller acts
    on differently:

    - `not_yet` — inside the legal envelope, outside the rollout. A waitlist.
    - `not_served` — outside the legal envelope. Not something waiting will fix.

    Conflating them would tell a Los Angeles seller their account is broken.
    """

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


@lru_cache(maxsize=1)
def _snapshot() -> dict:
    return json.loads(_SNAPSHOT_PATH.read_text())


@lru_cache(maxsize=1)
def areas() -> tuple:
    """Every area in the snapshot, in abbreviation order."""
    return tuple(_snapshot().get("areas", []))


@lru_cache(maxsize=1)
def _by_abbreviation() -> dict:
    return {area["abbreviation"]: area for area in areas()}


@lru_cache(maxsize=1)
def _lookup() -> dict:
    """Every string a seller might reasonably name an area by, mapped to its abbreviation.

    Hostname and description are included because they are what a seller can actually see: the
    hostname sits in `/area/<hostname>`, the description is the site's own title. The abbreviation
    is included so a resolved value round-trips. Subarea descriptions map to their *parent* area,
    because the names people actually use are often subareas — Craigslist calls our launch area
    "SF bay area", while "city of san francisco" is a subarea of it.

    Matching is exact on the normalized form, deliberately. Containment matching was tried and is
    unusable: three-letter abbreviations sit inside longer place names, so "berkeley" matches `ber`
    and `kel`, and "san francisco" matches `anc` and `fra`. A near miss instead earns a suggestion
    (`_unknown_message`), which is honest about the ambiguity rather than guessing through it.
    """
    table: dict = {}
    for area in areas():
        keys = [area["abbreviation"], area["hostname"], area["description"]]
        keys += [sub["description"] for sub in area.get("subareas") or []]
        for key in keys:
            table.setdefault(_normalize(key), area["abbreviation"])
    return table


def _normalize(said: object) -> str:
    """Fold what a seller typed to a comparison key: lowercase, and non-alphanumerics dropped.

    Dropping separators is what lets "SF bay area", "sf-bay-area" and "sfbayarea" all land on the
    same entry, and it is why a pasted URL reduces to something matchable after `_strip_url`.
    """
    return re.sub(r"[^a-z0-9]", "", str(said or "").lower())


def _strip_url(said: str) -> str:
    """The area out of a URL a seller pasted, or the input unchanged.

    Both shapes are live: `www.craigslist.org/area/<hostname>` is the canonical form, and
    `<hostname>.craigslist.org` still 301s to it, so a seller may hold either.
    """
    said = said.strip()
    path = re.search(r"/area/([a-z0-9]+)", said, re.IGNORECASE)
    if path:
        return path.group(1)
    host = re.match(r"(?:https?://)?([a-z0-9]+)\.craigslist\.org", said, re.IGNORECASE)
    if host:
        return host.group(1)
    return said


def resolve(said: object) -> str:
    """The Craigslist **abbreviation** for whatever a seller named — a hostname, a description, an
    abbreviation, or a URL they pasted.

    Never answers a hostname: the posting flow silently mis-files a hostname (see the module
    docstring), so the two must not be interchangeable anywhere above this function.
    """
    wanted = _normalize(_strip_url(str(said or "")))
    if not wanted:
        raise UnknownArea("no craigslist area was named")
    found = _lookup().get(wanted)
    if found is not None:
        return found
    raise UnknownArea(_unknown_message(str(said), wanted))


def _unknown_message(said: str, wanted: str) -> str:
    close = difflib.get_close_matches(wanted, _lookup().keys(), n=_SUGGESTIONS, cutoff=0.6)
    if not close:
        return f"no craigslist area called {said!r}"
    suggestions = ", ".join(sorted({_lookup()[key] for key in close}))
    return f"no craigslist area called {said!r} — did you mean {suggestions}?"


def get(abbreviation: str) -> dict | None:
    """One area's snapshot record, or None for an abbreviation the snapshot does not hold."""
    return _by_abbreviation().get(str(abbreviation or ""))


def subareas(abbreviation: str) -> list:
    """The area's subareas, in the order Craigslist offers them, or empty when it has none.

    The listing kit names these: a big metro's posting flow asks for a subarea before anything
    else, and it decides which buyers see the listing at all.
    """
    return list((get(abbreviation) or {}).get("subareas") or [])


def display_name(abbreviation: str) -> str:
    """The area as a place, for reading back to a seller — never a bare code, which answers nothing
    to "where will this be listed"."""
    area = get(abbreviation)
    if area is None:
        return str(abbreviation or "")
    return f"{area['description']} ({area['hostname']})"


def check_servable(abbreviation: str) -> None:
    """Refuse unless we serve this area, saying which kind of no it is.

    Speaks only about areas that exist: an abbreviation the snapshot does not hold raises
    `UnknownArea`, because reporting a typo as a jurisdiction we do not serve would send a seller
    to a waitlist that will never reach them.
    """
    area = get(abbreviation)
    if area is None:
        raise UnknownArea(f"no craigslist area called {abbreviation!r}")
    if abbreviation in CRAIGSLIST_AREAS:
        return
    if area.get("state") in SERVED_STATES:
        raise AreaNotAvailable(
            f"{display_name(abbreviation)} isn't somewhere I work yet — "
            f"right now that's {_served_names()}",
            reason="not_yet",
        )
    raise AreaNotAvailable(
        f"I can't sell in {display_name(abbreviation)} — I only work {_served_names()} for now",
        reason="not_served",
    )


def servable_areas() -> list:
    """The areas a seller may choose, in abbreviation order — what a refusal names back."""
    return sorted(CRAIGSLIST_AREAS)


def _served_names() -> str:
    return ", ".join(display_name(area) for area in servable_areas())


def _check_rollout_is_legal() -> None:
    """Every launched area's state must be inside the legal envelope.

    At import, not at first use: a rollout that has outrun the legal envelope is a mistake nobody
    should be able to ship, and the only way to make that true is to refuse to load.
    """
    for abbreviation in sorted(CRAIGSLIST_AREAS):
        area = _by_abbreviation().get(abbreviation)
        if area is None:
            raise RuntimeError(
                f"CRAIGSLIST_AREAS names {abbreviation!r}, which is not a craigslist area"
            )
        if area.get("state") not in SERVED_STATES:
            raise RuntimeError(
                f"CRAIGSLIST_AREAS names {abbreviation!r} in {area.get('state')!r}, which is not "
                f"in SERVED_STATES ({sorted(SERVED_STATES)}) — open the state before the area"
            )


_check_rollout_is_legal()
