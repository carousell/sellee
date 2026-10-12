"""The setup questions for where the seller sells, asked when the machine cannot vouch for it: the
country, then the timezone as a place rather than a zone name."""

from __future__ import annotations

from sellee import countries
from sellee.installer import region as region_guess
from sellee.installer.ui import Ui


def ask_basics(ui: Ui):
    """Ask which country outright, taking any of them: what carousell.ai can pay out is the
    backend's answer, so a list here would be the agent deciding something it does not know."""
    if not ui.interactive:
        return None
    region = _ask_country(ui)
    zone = ask_timezone(ui, region)
    return {"region": region, "timezone": zone} if zone else {"region": region}


def _ask_country(ui: Ui) -> str:
    """Ask for a country code until the answer is one the write door will take, and confirm it.

    The confirm is the point. A code of the right shape is always accepted, so the error no check
    can catch is a real country that is not theirs — "SA" typed for "SG" is two letters, is a
    country, provisions, and then prices every listing in the wrong currency. Naming it back is
    the only thing standing between that typo and a live mislabelled listing.

    A typed country *name* is resolved rather than rejected: the seller has already told us what
    they meant, and spelling it as a code for them is not the agent deciding where they may sell.
    """
    default = (region_guess.guess() or {}).get("region", "")
    while True:
        answer = ui.ask("Which country do you sell in?", default=default, lead=False).strip()
        # Spelled before the shape check: "UK" is two letters but not a code, and resolves to GB.
        # A real code is left alone by code_for_name, so "AU" is never read as a name.
        code = countries.code_for_name(answer) or answer.upper()
        if len(code) != 2 or not code.isalpha():
            ui.say("A country is its two-letter code, like US or CA.")
            continue
        if ui.confirm(f"You sell in {countries.label(code)}, correct?", default=True):
            return code


_SOMEWHERE_ELSE = "Somewhere else — type a city"


def ask_timezone(ui: Ui, region: str) -> str:
    """Ask for the seller's timezone as a place: a numbered list when there is more than one to
    offer, else a city or zone name typed. Returns "" when skipped."""
    machine = region_guess.system_timezone()
    places = region_guess.place_zones(region)
    listed_city = region_guess.city_label(places[0][1]) if places else ""
    # The stored zone is a claim about this machine, so its own zone is offered in any country.
    if machine and machine not in [zone for _, zone in places]:
        places = [(f"{region_guess.city_label(machine)} (this computer)", machine), *places]
    zones = [zone for _, zone in places]
    if len(zones) < 2:
        return _ask_city(ui, zones[0] if zones else "", listed_city)
    question = "Which of these is your timezone?"
    options = [label for label, _ in places] + [_SOMEWHERE_ELSE]
    if machine:
        picked = ui.choose(question, options, default_index=zones.index(machine), lead=False)
    else:
        # Nothing makes one place likelier than another, so Enter skips rather than picks.
        picked = ui.choose_optional(question, options, lead=False)
        if picked is None:
            return _no_timezone(ui)
    return zones[picked] if picked < len(zones) else _ask_city(ui, "", listed_city)


def _no_timezone(ui: Ui) -> str:
    ui.note("no timezone recorded — ask Sellee to set one any time")
    return ""


def _ask_city(ui: Ui, default: str, listed_city: str) -> str:
    """Ask for a city or zone name until it resolves to a storable zone, or "" when skipped. A
    city is confirmed before it is stored, because the match is by spelling alone. Few cities
    have a zone named after them, so one that has none is pointed at `listed_city`, a city of the
    seller's country that does."""
    question = "Timezone?" if default else "Which city are you in?"
    nearby = (
        f"a city in your timezone, like {listed_city}" if listed_city else "the nearest big city"
    )
    while True:
        answer = ui.ask(question, default=default, lead=False).strip()
        if not answer:
            return _no_timezone(ui)
        if answer == default or "/" in answer:
            reason = region_guess.zone_error(answer)
            if not reason:
                return answer
            ui.say(f"{reason} — zone names look like {default or 'Asia/Singapore'}.")
            continue
        zones = region_guess.zones_for_city(answer)
        if answer in zones:
            return answer
        if len(zones) == 1 and ui.confirm(f"{zones[0]}, correct?", default=True, lead=False):
            return zones[0]
        if len(zones) > 1:
            return zones[ui.choose("Which of these?", zones, lead=False)]
        if not zones:
            ui.say(f"No timezone is named after {answer!r} — try {nearby}.")
        question = "Which city are you in?"
