"""The runtime-settings registry: the one place a seller-facing behavior knob is defined.

A setting is distinct from the two other config homes. seller_config holds domain records
(basics, shipping zones, the origin address) — data the flows consult. The operator config file
holds install/operator knobs with restart semantics. A *setting* is a behavior knob the seller
changes at runtime, through a door, under a change protocol: the LLM may only propose, and every
apply happens in deterministic daemon code behind a human signal.

Each key's schema lives here as a SettingSpec — its parser/validator, human renderer, default,
description, take-effect note, and whether a change needs approval. The registry is at once the
validation source, the discoverability source (the /sellee card and get_settings read it), and the
LLM's vocabulary. Defaults live in code, not as rows: an unset key reads as its registry default,
so "changed from default" is a plain value comparison and a new setting needs no backfill.

Setting values are JSON-native (the store round-trips them through JSON), so a canonical value is a
list/number/string/bool, never a tuple — a consumer that wants a tuple builds one at its use site.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

from sellee import marketplaces
from sellee.browser import markets as market_adapters

log = logging.getLogger(__name__)

# An unaccepted proposal expires after this long — long enough that an approval notice sitting on a
# phone is still tappable hours later, short enough that a stale window doesn't linger for days.
PROPOSAL_TTL_SEC = 24 * 3600

# Settings always shown on the /sellee card even at their default — the discoverable headline set.
# Everything else appears on the card only once changed from its default (the card scales with
# customization, not catalog size).
#
# crosslist_markets earns a place at its default because its default is "off" and nothing else on
# the card would ever hint that listing elsewhere is possible; a knob that shapes wording is
# discoverable from the wording, but a market the seller has never been told about is not.
#
# watch_browser earns its place for the same reason and one more: where the work happens is not
# something any wording the agent produces could hint at, and the card carries the button that
# flips it — a toggle whose current state is not legible right above it is a coin toss.
CARD_HEADLINE = ("quiet_hours", "crosslist_markets", "watch_browser")

# The door tokens. A callback carries the change id and one of these choices (the channel encodes it
# as "<change_id>:<choice>"); a text fast path is "<verb> <change_id>". Both are LLM-free doors: an
# authenticated surface plus a deterministic parse and a deterministic apply.
CB_APPROVE = "setapprove"
CB_CANCEL = "setcancel"
CB_UNDO = "setundo"
CALLBACK_CHOICES = frozenset({CB_APPROVE, CB_CANCEL, CB_UNDO})

TEXT_APPROVE = "approve"
TEXT_CANCEL = "cancel"
TEXT_UNDO = "undo"
TEXT_VERBS = frozenset({TEXT_APPROVE, TEXT_CANCEL, TEXT_UNDO})

# The decision a door reached, returned by the decision router so every surface renders one wording.
DECIDE_APPROVE = "approve"
DECIDE_CANCEL = "cancel"
DECIDE_UNDO = "undo"


class SettingError(ValueError):
    """A proposed setting value is invalid. Its message is caller-facing — the LLM round-trips it
    conversationally — and never carries a secret (setting values are non-secret)."""


@dataclass(frozen=True)
class SettingSpec:
    """One setting's schema. `parse` canonicalizes raw input (raising SettingError on bad input);
    `render` turns a canonical value into seller-facing text. `default` is the value an unset key
    reads as. `requires_approval` routes a change through the approve door instead of applying it
    immediately."""

    key: str
    label: str
    parse: Callable[[object], object]
    render: Callable[[object], str]
    default: object
    description: str
    take_effect: str
    requires_approval: bool = False


_REGISTRY: dict[str, SettingSpec] = {}


def register(spec: SettingSpec) -> SettingSpec:
    if spec.key in _REGISTRY:
        raise ValueError(f"duplicate setting registration: {spec.key}")
    _REGISTRY[spec.key] = spec
    return spec


def unregister(key: str) -> None:
    """Remove a registered setting. A test hook for exercising registry-driven paths (e.g. the
    immediate-apply path, which v1's only real setting — quiet_hours, approval-gated — cannot);
    production settings are permanent."""
    _REGISTRY.pop(key, None)


def get_spec(key: str) -> SettingSpec | None:
    return _REGISTRY.get(key)


def all_specs() -> list[SettingSpec]:
    """Every registered spec, in registration order (drives card / get_settings ordering)."""
    return list(_REGISTRY.values())


def canonical_json(value: object) -> str:
    """The stable JSON encoding used for value equality — so "changed from default" and the
    "proposing the current value" short-circuit compare canonically, not by Python identity."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# --- read helpers (registry default when unset) ----------------------------------------------


def get(store, key: str) -> object:
    """One setting's effective value: the stored value if set, else the registry default. Raises
    KeyError for an unregistered key — callers pass a literal registry key."""
    spec = _REGISTRY[key]
    stored = store.get_setting(key)
    return spec.default if stored is None else stored


def effective(store) -> dict:
    """Every registered setting's effective value, keyed by setting key (registry default when
    unset). An orphan stored key — one whose registry entry is gone — is ignored here (a stale row
    never crashes a render), warned once so it's visible."""
    stored = store.get_all_settings()
    for key in stored:
        if key not in _REGISTRY:
            log.warning("ignoring stored setting %r with no registry entry", key)
    return {spec.key: stored.get(spec.key, spec.default) for spec in _REGISTRY.values()}


def is_default(key: str, value: object) -> bool:
    return canonical_json(value) == canonical_json(_REGISTRY[key].default)


def check_for_seller(key: str, value: object, store) -> None:
    """Validate an already-parsed value against seller state, raising SettingError if it cannot
    apply. Most settings are valid or invalid on their own terms; a marketplace list is not, because
    which marketplaces exist depends on where the seller sells."""
    if key == "crosslist_markets":
        _check_crosslist_markets(value, store)


# --- discoverability renderers (F10) ---------------------------------------------------------


def card_lines(store) -> list[str]:
    """The /sellee card's settings lines: every changed-from-default setting plus the headline set,
    values inline, then a trailer counting the rest still at their defaults."""
    values = effective(store)
    shown = [
        spec
        for spec in _REGISTRY.values()
        if spec.key in CARD_HEADLINE or not is_default(spec.key, values[spec.key])
    ]
    lines = [f"• {spec.label}: {spec.render(values[spec.key])}" for spec in shown]
    remaining = len(_REGISTRY) - len(shown)
    if remaining > 0:
        plural = "settings" if remaining != 1 else "setting"
        lines.append(f"{remaining} more {plural} at defaults — ask me about settings.")
    return lines


def prompt_block(store) -> str:
    """A compact settings block for the channel-pass prompt: the LLM's near-context vocabulary. It
    states the propose-only rule so the model never narrates an apply it cannot perform."""
    values = effective(store)
    lines = [
        "Settings you can change for the seller (propose only — you cannot apply; the seller "
        "approves or it applies automatically, per the setting):"
    ]
    for spec in _REGISTRY.values():
        lines.append(f"- {spec.key}: {spec.render(values[spec.key])} — {spec.description}")
    lines.append("Change one with propose_setting_change; list them all with get_settings.")
    return "\n".join(lines)


def pending_view(store) -> list[dict]:
    """Live proposals as seller-facing rows — the change id (the CLI door needs it), the setting,
    and the current → proposed rendering. Surfaced by get_catchup and `settings list`."""
    rows = []
    for change in store.list_pending_changes():
        spec = get_spec(change["key"])
        rows.append(
            {
                "change_id": change["change_id"],
                "key": change["key"],
                "label": spec.label if spec else change["key"],
                "current": spec.render(change["prior_value"]) if spec else change["prior_value"],
                "proposed": spec.render(change["value"]) if spec else change["value"],
                "proposed_ts": change["proposed_ts"],
            }
        )
    return rows


def describe(store) -> list[dict]:
    """Every registered setting as data for get_settings: value, default, description, and whether
    a change to it requires approval — the LLM's tail-discovery source."""
    values = effective(store)
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "value": values[spec.key],
            "rendered": spec.render(values[spec.key]),
            "default": spec.default,
            "description": spec.description,
            "take_effect": spec.take_effect,
            "requires_approval": spec.requires_approval,
        }
        for spec in _REGISTRY.values()
    ]


# --- door copy + the deterministic decision router -------------------------------------------


def approval_notice(spec: SettingSpec, change_id: str, value: object, prior_value: object) -> tuple:
    """The (text, controls) for a held change's approval notice. The change id rides in both the
    buttons and the text, so a button-less surface (or a catchup delivery that drops the keyboard)
    still carries the token the text fast path needs."""
    text = (
        f"Approve change to {spec.label.lower()}: "
        f"{spec.render(prior_value)} → {spec.render(value)}?\n"
        f"{spec.take_effect}\n"
        f"Tap Approve/Cancel below, or reply: approve {change_id}"
    )
    controls = [["Approve", f"{change_id}:{CB_APPROVE}"], ["Cancel", f"{change_id}:{CB_CANCEL}"]]
    return text, controls


def echo_notice(spec: SettingSpec, change_id: str, value: object, prior_value: object) -> tuple:
    """The (text, controls) for the echo notice after a change applies — carries the Undo door."""
    text = (
        f"{spec.label} set to {spec.render(value)} (was {spec.render(prior_value)}).\n"
        f"Undo: tap below or reply: undo {change_id}"
    )
    controls = [["Undo", f"{change_id}:{CB_UNDO}"]]
    return text, controls


def undo_confirmation(spec: SettingSpec, value: object) -> str:
    return f"{spec.label} reverted to {spec.render(value)}."


def _is_channel_origin(decided_via: str) -> bool:
    """A button tap or a text token comes in over the channel, which then replies synchronously; a
    CLI ('cli') or an auto-apply ('auto') has no such reply path."""
    return decided_via in ("button", "token")


def _current_status_message(current: str | None) -> str:
    return {
        "applied": "That change was already applied.",
        "cancelled": "That change was already cancelled.",
        "superseded": "That request was replaced by a newer one — ask me again.",
        "expired": "That request expired — ask me again.",
    }.get(current or "", "That change wasn't found — ask me again.")


def decide(store, bus, *, change_id: str, decision: str, decided_via: str) -> dict:
    """The one deterministic apply path every door funnels through — button, text token, or CLI.
    Reads the referenced change, applies/cancels/undoes it in the store (which re-checks state under
    its own lock), publishes the outcome event, and returns {status, message[, key]} for the door to
    render. No LLM is involved past the id: the parse and the apply are both deterministic."""
    change = store.get_pending_change(change_id)
    if change is None:
        return {"status": "unknown", "message": "That change wasn't found — ask me again."}
    spec = get_spec(change["key"])
    if spec is None:
        return {"status": "unknown", "message": "That setting no longer exists."}
    key = change["key"]

    channel = _is_channel_origin(decided_via)

    if decision == DECIDE_APPROVE:
        # A channel door (button/text) has a synchronous reply path, so it carries the confirmation
        # itself (with the Undo button) — queuing an echo notice too would deliver it twice to the
        # same chat. A CLI/auto decision has no channel reply, so it queues the echo notice as the
        # channel's only path to the confirmation.
        if channel:
            result = store.approve_setting_change(
                change_id, decided_via=decided_via, ttl_sec=PROPOSAL_TTL_SEC
            )
        else:
            text, controls = echo_notice(spec, change_id, change["value"], change["prior_value"])
            result = store.approve_setting_change(
                change_id,
                decided_via=decided_via,
                notice_text=text,
                notice_controls=controls,
                ttl_sec=PROPOSAL_TTL_SEC,
            )
        if result["status"] == "applied":
            publish_changed(bus, spec, change_id, result["value"], result["prior_value"])
            out = {
                "status": "applied",
                "key": key,
                "message": f"Applied — {spec.label.lower()} is now {spec.render(result['value'])}.",
            }
            if channel:
                out["controls"] = [["Undo", f"{change_id}:{CB_UNDO}"]]
            return out
        if result["status"] == "expired":
            bus.publish("setting.expired", {"change_id": change_id, "key": key})
            return {
                "status": "expired",
                "key": key,
                "message": "That request expired — ask me again.",
            }
        return {
            "status": "not_pending",
            "key": key,
            "message": _current_status_message(result.get("current")),
        }

    if decision == DECIDE_CANCEL:
        result = store.cancel_setting_change(
            change_id, decided_via=decided_via, ttl_sec=PROPOSAL_TTL_SEC
        )
        if result["status"] == "cancelled":
            bus.publish("setting.cancelled", {"change_id": change_id, "key": key})
            return {
                "status": "cancelled",
                "key": key,
                "message": f"Cancelled — {spec.label.lower()} unchanged.",
            }
        if result["status"] == "expired":
            bus.publish("setting.expired", {"change_id": change_id, "key": key})
            return {
                "status": "expired",
                "key": key,
                "message": "That request expired — ask me again.",
            }
        return {
            "status": "not_pending",
            "key": key,
            "message": _current_status_message(result.get("current")),
        }

    if decision == DECIDE_UNDO:
        # Same rule as approve: a channel door replies synchronously (no confirmation notice); a
        # CLI/auto undo queues the confirmation notice so the channel still learns of it.
        if channel:
            result = store.undo_setting_change(change_id, decided_via=decided_via)
        else:
            confirm = undo_confirmation(spec, change["prior_value"])
            result = store.undo_setting_change(
                change_id, decided_via=decided_via, notice_text=confirm
            )
        if result["status"] == "undone":
            publish_changed(bus, spec, result["change_id"], result["value"], result["prior_value"])
            restored = spec.render(result["value"])
            return {
                "status": "undone",
                "key": key,
                "message": f"Reverted — {spec.label.lower()} back to {restored}.",
            }
        reason = result.get("reason")
        message = {
            "superseded": "Can't undo — a newer change has replaced it.",
            "not_applied": "That change wasn't applied, so there's nothing to undo.",
        }.get(reason, "That change wasn't found — nothing to undo.")
        return {"status": "not_undoable", "key": key, "message": message}

    return {"status": "unknown", "message": "Unrecognized settings action."}


def decode_raw(raw: object) -> object:
    """Turn a value that arrived as text into the structure the registry parser expects.

    A setting's shape is per-key, so neither the MCP tool nor the CLI can declare a type for it,
    and both surfaces hand us text: an MCP client commonly sends a structured value JSON-encoded
    ("[23, 9]"), and a shell argument is always a string. Anything that is not JSON — a bare word
    for a text setting — passes through untouched.
    """
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return raw
    return raw


def set_now(store, bus, *, key: str, raw_value: object, decided_via: str = "cli") -> dict:
    """Validate and apply a setting immediately, for a door where the request *is* the consent.

    The approval gate exists to stop the model changing things on the seller's behalf. Someone
    typing at their own terminal — or tapping a button in their own chat — has already given the
    signal that gate waits for, so this path skips the round-trip but skips nothing else: the same
    registry parser and the same seller-state check run here as on the propose path, and the prior
    value is recorded, so the change still shows up in the ledger with a working Undo.

    A channel door (a button, a text token) replies synchronously and renders its own controls, so
    queuing the echo notice too would deliver the same confirmation twice to the same chat. A
    CLI/auto caller has no reply path, so the echo notice is the channel's only way of hearing
    about it. The same rule, for the same reason, as `decide`.

    Raises SettingError for an unknown key or a value the registry refuses; the message is
    caller-facing.
    """
    spec = get_spec(key)
    if spec is None:
        known = ", ".join(sorted(_REGISTRY))
        raise SettingError(f"unknown setting {key!r} — the settings are: {known}")
    value = spec.parse(decode_raw(raw_value))
    check_for_seller(key, value, store)

    current = get(store, key)
    rendered = spec.render(value)
    if canonical_json(value) == canonical_json(current):
        return {
            "status": "unchanged",
            "key": key,
            "value": value,
            "rendered": rendered,
            "message": f"{spec.label} is already {rendered}.",
        }

    change_id = store.new_change_id()
    if _is_channel_origin(decided_via):
        store.apply_setting_now(
            key, value, change_id=change_id, prior_value=current, decided_via=decided_via
        )
    else:
        text, notice_controls = echo_notice(spec, change_id, value, current)
        store.apply_setting_now(
            key,
            value,
            change_id=change_id,
            prior_value=current,
            decided_via=decided_via,
            notice_text=text,
            notice_controls=notice_controls,
        )
    publish_changed(bus, spec, change_id, value, current)
    return {
        "status": "applied",
        "change_id": change_id,
        "key": key,
        "value": value,
        "rendered": rendered,
        "prior_value": current,
        "message": f"{spec.label} set to {rendered}. {spec.take_effect}",
    }


def expire_stale_proposals(store, bus, now: float | None = None) -> int:
    """Sweep proposals older than the TTL to expired, publishing setting.expired per one. An
    expired proposal is answered when tapped (never re-fired) — the seller starts fresh. Returns the
    count expired."""
    now = time.time() if now is None else now
    expired = store.expire_pending_changes(now - PROPOSAL_TTL_SEC)
    for change in expired:
        bus.publish("setting.expired", {"change_id": change["change_id"], "key": change["key"]})
    return len(expired)


def publish_changed(bus, spec, change_id, value, prior_value) -> None:
    bus.publish(
        "setting.changed",
        {
            "change_id": change_id,
            "key": spec.key,
            "value": value,
            "rendered": spec.render(value),
            "prior_value": prior_value,
        },
    )


# --- the v1 inventory: quiet_hours only ------------------------------------------------------

# The canonical value is a [start, end] pair of HHMM integers (2300 = 23:00, 930 = 09:30, 0 =
# midnight) — readable at a glance and minute-granular, so a seller can set, say, 22:30–07:15.
# Equal values disable the window. The pacing engine works in minutes since midnight, so
# quiet_window_minutes() converts at the boundary.
_QUIET_HELP = (
    "quiet hours must be a [start, end] pair of times as HHMM integers — e.g. [2300, 930] for "
    "11pm to 9:30am, or whole hours [23, 9]; equal values disable it"
)


def _to_hhmm(elem: object) -> int:
    """Canonicalize one end of the window to an HHMM integer. Accepts a whole hour 0..24 (23 →
    2300), an HHMM integer 0..2400 with valid minutes (930 → 09:30), or an 'HH:MM'/'HHMM' string.
    Anything else raises SettingError."""
    if isinstance(elem, bool):
        raise SettingError(_QUIET_HELP)
    if isinstance(elem, int):
        if 0 <= elem <= 24:
            return elem * 100
        if 100 <= elem <= 2400 and elem % 100 <= 59:
            return elem
        raise SettingError(_QUIET_HELP)
    if isinstance(elem, str):
        s = elem.strip()
        try:
            if ":" in s:
                hh, mm = s.split(":", 1)
                h, m = int(hh), int(mm)
                if 0 <= h <= 24 and 0 <= m <= 59:
                    return h * 100 + m
            else:
                return _to_hhmm(int(s))
        except (ValueError, TypeError):
            pass
    raise SettingError(_QUIET_HELP)


def _parse_quiet_hours(raw: object) -> list:
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return [_to_hhmm(raw[0]), _to_hhmm(raw[1])]
    raise SettingError(_QUIET_HELP)


def _render_quiet_hours(value: object) -> str:
    start, end = value
    if start == end:
        return "disabled"
    return f"{start // 100:02d}:{start % 100:02d}–{end // 100:02d}:{end % 100:02d}"


def _hhmm_to_minutes(hhmm: int) -> int:
    return (hhmm // 100) * 60 + (hhmm % 100)


def quiet_window_minutes(store) -> tuple:
    """The quiet-hours window as (start, end) minutes since midnight — the pacing engine's unit.
    The stored/rendered value is HHMM; this is the one place that converts."""
    start, end = get(store, "quiet_hours")
    return _hhmm_to_minutes(start), _hhmm_to_minutes(end)


register(
    SettingSpec(
        key="quiet_hours",
        label="Quiet hours",
        parse=_parse_quiet_hours,
        render=_render_quiet_hours,
        default=[2300, 800],  # 23:00–08:00
        description="A nightly window that holds routine updates until it ends (urgent decisions "
        "still come through). A [start, end] pair of HHMM times (2300 = 11pm, 930 = 9:30am); equal "
        "values disable it.",
        take_effect="takes effect immediately for new marketplace sends; already-queued notices "
        "re-check at the next drain.",
        requires_approval=True,
    )
)


# --- style: how the seller likes to deal ------------------------------------------------------
#
# Two knobs, and they work in opposite places. `persona` is read at compose time and shapes wording
# only — it can never change a decision, a routing choice, or a number. `firmness` is never read at
# compose time; it feeds numbers to the negotiation engine. Both are low-stakes, so a proposal
# applies immediately and the seller gets an echo notice with an Undo.

PERSONA_MAX_CHARS = 280

_FIRMNESS_LEVELS = ("soft", "balanced", "firm", "hardline")
_FIRMNESS_HELP = f"firmness must be one of: {', '.join(_FIRMNESS_LEVELS)}"


def _parse_persona(raw: object) -> str:
    if not isinstance(raw, str):
        raise SettingError("persona must be text (or an empty string to clear it)")
    text = raw.strip()
    if len(text) > PERSONA_MAX_CHARS:
        raise SettingError(f"persona must be at most {PERSONA_MAX_CHARS} characters")
    return text


def _render_persona(value: object) -> str:
    return str(value) if value else "none set"


def _parse_firmness(raw: object) -> str:
    if isinstance(raw, str) and raw.strip().lower() in _FIRMNESS_LEVELS:
        return raw.strip().lower()
    raise SettingError(_FIRMNESS_HELP)


def _render_firmness(value: object) -> str:
    return str(value)


register(
    SettingSpec(
        key="persona",
        label="Persona",
        parse=_parse_persona,
        render=_render_persona,
        default="",
        description='A free-text steer on voice, e.g. "cheeky, give lowballers a hard time". '
        "Guidance for wording only — it never changes a price, a decision, or an escalation.",
        take_effect="applies to the next message composed.",
    )
)
register(
    SettingSpec(
        key="firmness",
        label="Negotiation firmness",
        parse=_parse_firmness,
        render=_render_firmness,
        default="balanced",
        description="How hard to haggle: soft | balanced | firm | hardline. Sets how many "
        "counters to make, how low an offer counts as a lowball, and how many lowballs to "
        "tolerate. A per-item counter setting still wins over it.",
        take_effect="applies to the next offer decided.",
    )
)


# --- where a listing goes -----------------------------------------------------------------------
#
# The marketplaces to list on *besides* carousell.ai. The rail is not a member: it is where every
# listing goes, guaranteed by the flow itself, so naming it here would only be a value the validator
# has to special-case. Empty — the default — means carousell.ai alone, so an upgrade changes
# nobody's behaviour.
#
# A market is accepted only if something can actually drive it (an adapter plus a publish recipe).
# Refusing at write time rather than skipping at publish time keeps the failure where the seller can
# see it: a setting that lists a market nothing publishes to reads as a promise being kept.


def _market_names(markets) -> str:
    return ", ".join(marketplaces.display_name(market) for market in markets)


def _crosslist_help() -> str:
    supported = market_adapters.supported_markets()
    if not supported:
        return "no other marketplaces are supported yet — carousell.ai only"
    return f"the only other marketplace I can list on is {_market_names(supported)}"


def _parse_crosslist_markets(raw: object) -> list:
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, (list, tuple)):
        raise SettingError(f"this must be a list of marketplaces — {_crosslist_help()}")
    supported = market_adapters.supported_markets()
    out = set()
    for entry in raw:
        market = str(entry).strip().lower()
        if not market:
            continue
        if market not in supported:
            raise SettingError(f"I can't list on {market!r} — {_crosslist_help()}")
        out.add(market)
    return sorted(out)


def _check_crosslist_markets(value: object, store) -> None:
    """Refuse a marketplace that exists but has nowhere to put this seller's listing.

    Whether we can drive a marketplace is a fact about our code, which the parser settles. Whether
    it operates where the seller sells is a fact about them, so it is checked here — a US seller
    cannot be listed on Carousell, which runs no US site.
    """
    region = store.seller_region()
    if not region:
        raise SettingError(
            "I don't know which country you sell in yet, so I can't tell which marketplaces are "
            "available — tell me your region and I'll set this up"
        )
    available = market_adapters.publishable_markets(region)
    unavailable = [market for market in value if market not in available]
    if not unavailable:
        return
    if available:
        raise SettingError(
            f"{_market_names(unavailable)} isn't available for {region} accounts — "
            f"I can list on {_market_names(available)}"
        )
    raise SettingError(
        f"{_market_names(unavailable)} isn't available for {region} accounts, and no other "
        "marketplace is either yet — carousell.ai only"
    )


def _render_crosslist_markets(value: object) -> str:
    if not value:
        return "none — carousell.ai only"
    return ", ".join(marketplaces.display_name(market) for market in value)


def crosslist_markets(store) -> list:
    """The external marketplaces the seller has enabled, filtered to the ones still publishable.

    The filter matters after the fact: a stored market can stop being publishable — an adapter
    withdrawn, or the seller's region changed to one it does not serve — and a stale id must not
    become an eligible publish.
    """
    publishable = market_adapters.publishable_markets(store.seller_region())
    return [market for market in get(store, "crosslist_markets") if market in publishable]


register(
    SettingSpec(
        key="crosslist_markets",
        label="Enabled marketplaces",
        parse=_parse_crosslist_markets,
        render=_render_crosslist_markets,
        default=[],
        description="Other marketplaces to list on as well as carousell.ai. Each one is published "
        "in the browser after the carousell.ai listing is live, and the link is sent over when it "
        "is up. Empty means carousell.ai only.",
        take_effect="applies to items listed from now on; anything already listed is picked up "
        "too.",
        requires_approval=True,
    )
)


# --- the browser window -------------------------------------------------------------------------
#
# Whether the agent's Chrome window is brought to the front when it opens a page the seller has to
# act on (a marketplace sign-in), or left in the background. On by default: a window the seller
# was asked to sign in to but cannot find is the failure this knob exists to prevent. Low-stakes
# and purely the seller's own UX, so a change applies immediately with an Undo.

_BOOL_TRUE = frozenset({"true", "yes", "on"})
_BOOL_FALSE = frozenset({"false", "no", "off"})
_RAISE_BROWSER_HELP = "this must be true or false (bring my window to the front, or not)"


def _parse_bool(raw: object, help_text: str) -> bool:
    """A yes/no setting's value. The words are accepted alongside the JSON booleans because both
    doors hand us text: a seller types "on", and a shell argument is always a string."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        word = raw.strip().lower()
        if word in _BOOL_TRUE:
            return True
        if word in _BOOL_FALSE:
            return False
    raise SettingError(help_text)


def _parse_raise_browser(raw: object) -> bool:
    return _parse_bool(raw, _RAISE_BROWSER_HELP)


def _render_raise_browser(value: object) -> str:
    if value:
        return "comes to the front when I open a page for you"
    return "stays in the background"


register(
    SettingSpec(
        key="raise_browser",
        label="Browser window",
        parse=_parse_raise_browser,
        render=_render_raise_browser,
        default=True,
        description="Whether my own Chrome window comes to the front when I open a page you need "
        "to act on (like a marketplace sign-in), or stays in the background for you to find. "
        "true or false.",
        take_effect="applies to the next page I open for you.",
    )
)


# --- watching the work ----------------------------------------------------------------------
#
# Whether the seller watches the agent work, or the agent stays out of the way. A sibling of
# raise_browser rather than part of it, because the two answer different questions: that one is
# about a page the *seller* must act on, this one is about seeing work the agent does for itself.
#
# Two mechanisms, deliberately unequal. The tab-follow (browser/client.py) makes the agent's tab the
# active tab of its window after every navigation, so a window parked on a second screen is a live
# view; it costs one extra call per navigation and works everywhere, container included. The window
# raise (browser/window.py) is macOS-only and happens at three moments only — turning this on, a
# reply going out, and a browser pass starting. A read tick never raises: the lane runs every few
# minutes, and a window that jumps the seller's focus while they work is the thing they turn off.

_WATCH_BROWSER_HELP = "this must be true or false (watch me work, or let me work in the background)"


def _parse_watch_browser(raw: object) -> bool:
    return _parse_bool(raw, _WATCH_BROWSER_HELP)


def _render_watch_browser(value: object) -> str:
    if value:
        return "on — you'll see the page I'm working on"
    return "off — I work in the background"


register(
    SettingSpec(
        key="watch_browser",
        label="Watch mode",
        parse=_parse_watch_browser,
        render=_render_watch_browser,
        default=False,
        description="Whether you watch me work. On, my Chrome window comes to the front when I "
        "start something (answering a buyer, putting up a listing) and its tab follows whatever "
        "page I'm on, so you can see what I'm doing. Off, I work in the background. Reading "
        "inboxes never takes your focus either way, and bringing the window forward only works on "
        "a Mac. true or false.",
        take_effect="applies to the next page I open.",
    )
)
