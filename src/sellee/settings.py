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

from sellee import connectables, craigslist_areas, marketplaces
from sellee.browser import markets as market_adapters

log = logging.getLogger(__name__)

# An unaccepted proposal expires after this long — long enough that an approval notice sitting on a
# phone is still tappable hours later, short enough that a stale window doesn't linger for days.
PROPOSAL_TTL_SEC = 24 * 3600

# Settings always shown on the /sellee card even at their default — the discoverable headline set.
# Everything else appears on the card only once changed from its default (the card scales with
# customization, not catalog size).
#
# connected_markets earns a place at its default because its default is "off" and nothing else on
# the card would ever hint that other marketplaces are possible.
#
# watch_browser earns its place for the same reason and one more: where the work happens is not
# something any wording the agent produces could hint at, and the card carries the button that
# flips it — a toggle whose current state is not legible right above it is a coin toss.
CARD_HEADLINE = ("quiet_hours", "watch_browser")

# Settings the card renders in a section of their own, and which `card_lines` therefore leaves out.
# connected_markets is the only one: the Connections block lists every marketplace with its state
# and the buttons that change it — printing both would show the same fact twice.
CARD_OWN_SECTION = frozenset({"connected_markets"})

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
    if key == "connected_markets":
        _check_connected_markets(value, store)
    elif key == "craigslist_area":
        _check_craigslist_area(value, store)


# --- discoverability renderers (F10) ---------------------------------------------------------


def card_lines(store) -> list[str]:
    """The /sellee card's settings lines: every changed-from-default setting plus the headline set,
    values inline, then a trailer counting the rest still at their defaults."""
    values = effective(store)
    shown = [
        spec
        for spec in _REGISTRY.values()
        if spec.key not in CARD_OWN_SECTION
        and (spec.key in CARD_HEADLINE or not is_default(spec.key, values[spec.key]))
    ]
    lines = [f"• {spec.label}: {spec.render(values[spec.key])}" for spec in shown]
    remaining = len(_REGISTRY) - len(shown) - len(CARD_OWN_SECTION)
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
            _request_signins(store, key, result["value"], result["prior_value"])
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


def _request_signins(store, key: str, value, prior) -> None:
    """Ask for a sign-in on any marketplace this change just switched on.

    The gap this closes: tapping **Connect** in the card writes `connected_markets` *and* queues
    the sign-in, because on-and-signed-in are one intent. Approving the identical write proposed by
    the model wrote the setting and queued nothing — so up to five minutes later the read lane
    probed a market nobody had signed into and reported it signed out, which is a true statement
    that reads like a fault and does not say what to do.

    Newly-added markets only, and their mailboxes too where they need one: a market already on is
    already being served, and re-requesting would navigate the shared tab for nothing.
    """
    if key != "connected_markets":
        return
    from sellee.store.browser import CONNECT_MODE_OPEN

    added = [market for market in (value or []) if market not in (prior or [])]
    for market in added:
        store.request_connect(market, CONNECT_MODE_OPEN)
        mail_target = connectables.mail_target_for(market)
        if mail_target and not store.mail_ready(market):
            # Queued alongside rather than after: the connect lane serves oldest first, so the
            # site is opened before the mailbox without either needing to know about the other.
            store.request_connect(mail_target, CONNECT_MODE_OPEN)


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
        description="A nightly window that holds routine updates and outreach until it ends — "
        "follow-ups, nudges and new listings wait for morning. Buyers who message you are still "
        "answered at any hour, and urgent decisions still come through. A [start, end] pair of "
        "HHMM times (2300 = 11pm, 930 = 9:30am); equal values disable it.",
        take_effect="takes effect immediately for new marketplace outreach; already-queued notices "
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


# --- which marketplaces the agent works ------------------------------------------------------
#
# The marketplaces the seller has connected *besides* carousell.ai. The rail is not a member —
# every listing goes there by the flow itself. Empty (the default) means carousell.ai alone.
#
# This list is the switch for everything the agent does on a marketplace — reading the inbox,
# answering buyers, surveying, signing in — not only publishing. Every consumer reads it at its
# own decision point, so removing a market stops work already queued.
#
# A market is accepted only if something can actually drive it. Refusing at write time keeps the
# failure where the seller can see it.


def _market_names(markets) -> str:
    return ", ".join(marketplaces.display_name(market) for market in markets)


def _connected_help() -> str:
    drivable = market_adapters.drivable_markets()
    if not drivable:
        return "no other marketplaces are supported yet — carousell.ai only"
    return f"the marketplaces I can work are {_market_names(drivable)}"


def _parse_connected_markets(raw: object) -> list:
    """Parse a marketplace list. Pure, per the settings contract: it knows what we can drive, which
    is a fact about our code, and nothing about where this seller sells — that is
    `_check_connected_markets`, which runs against the store."""
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, (list, tuple)):
        raise SettingError(f"this must be a list of marketplaces — {_connected_help()}")
    drivable = market_adapters.drivable_markets()
    out = set()
    for entry in raw:
        market = str(entry).strip().lower()
        if not market:
            continue
        if market not in drivable:
            raise SettingError(f"I can't work {market!r} — {_connected_help()}")
        out.add(market)
    return sorted(out)


def _check_connected_markets(value: object, store) -> None:
    """Refuse a marketplace that exists but has no site where this seller sells.

    Whether we can drive a marketplace is a fact about our code, which the parser settles. Whether
    it operates where the seller sells is a fact about them, so it is checked here — a US seller
    cannot be connected to Carousell, which runs no US site.

    A missing region is deliberately not refused on its own. A marketplace that serves everywhere
    resolves without one, so the old blanket refusal would have turned away a market that is
    genuinely available; a region only has to be known for the markets that actually need one, and
    those fail below by being absent from `available`.
    """
    region = store.seller_region()
    available = market_adapters.connectable_markets(region)
    unavailable = [market for market in value if market not in available]
    if not unavailable:
        return
    where = f"{region} accounts" if region else "an unset region"
    if available:
        raise SettingError(
            f"{_market_names(unavailable)} isn't available for {where} — "
            f"I can work {_market_names(available)}"
        )
    if not region:
        raise SettingError(
            "I don't know which country you sell in yet, so I can't tell which marketplaces are "
            "available — tell me your region and I'll set this up"
        )
    raise SettingError(
        f"{_market_names(unavailable)} isn't available for {where}, and no other "
        "marketplace is either yet — carousell.ai only"
    )


def _render_connected_markets(value: object) -> str:
    if not value:
        return "none — carousell.ai only"
    return ", ".join(marketplaces.display_name(market) for market in value)


def connected_markets(store) -> list:
    """The marketplaces the seller has connected — their intent, exactly as stored.

    This is the read-at-use answer to "does the seller want anything done on this marketplace at
    all". Every lane, tool and door that touches one asks it at its own decision point, which is
    what makes removing a market stop work already queued against it.

    Deliberately *not* filtered through what we can currently drive, unlike `publish_markets`. The
    two are different questions and the filter would conflate them: whether an adapter exists is
    already answered where it matters (the lane skips a market without one, the sink refuses a send
    on one), while quietly dropping a market here would make it vanish from the seller's own list of
    connected marketplaces after a release withdrew an adapter, with nothing to tell them why. The
    parser is what keeps nonsense out of the list in the first place.
    """
    return list(get(store, "connected_markets"))


def publish_markets(store) -> list:
    """The connected marketplaces we can also *publish* to — the ones with a recipe and a site.

    Narrower than `connected_markets` on purpose: connecting a marketplace turns on reading it and
    answering buyers, which needs no publish recipe. A market missing one is worked but not listed
    to, rather than being unavailable entirely.

    **And narrower again for a market whose buyers arrive as mail.** Publishing to Craigslist puts
    a real, public advertisement up with an email address on it. If that mailbox is not connected,
    every buyer who writes gets silence — so the listing is worse than no listing, and the seller
    finds out from a buyer rather than from us.

    Gating it *here* is deliberate and it is what makes the rule hold everywhere. Every route to a
    live Craigslist post runs through this list: the `queue_marketplace_publish` tool, `sellee pass
    run publish`, the crosslist fan-out (whose `pending_pairs` **backfills the whole catalogue** the
    moment a market becomes publishable), the MCP proxy, and the healthcheck's login probes. One
    filter closes all of them; a check added at each door would be one door away from a gap.
    """
    publishable = market_adapters.publishable_markets(store.seller_region())
    out = []
    for market in connected_markets(store):
        if market not in publishable:
            continue
        if market in connectables.MAIL_MARKETS and not store.mail_ready(market):
            continue
        out.append(market)
    return out


register(
    SettingSpec(
        key="connected_markets",
        label="Connected marketplaces",
        parse=_parse_connected_markets,
        render=_render_connected_markets,
        default=[],
        description="Marketplaces I work as well as carousell.ai. On a connected one I list your "
        "items, send the link over, and answer your buyers. Craigslist is the exception: its "
        "buyers email you, so it takes two sign-ins (the site, then that mailbox) and I pass on "
        "what buyers say — but Craigslist does not carry my replies back to them, so answering "
        "Craigslist buyers is yours. Empty means carousell.ai only.",
        take_effect="applies from now on: I start on a market you add and stop on one you remove, "
        "including work already queued.",
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


# --- where craigslist listings go -------------------------------------------------------------

# Craigslist's unit of geography is one of hundreds of city sites, not a country, so it needs a
# per-seller answer that `resolve_domain` (which is keyed by country) cannot give. This is the only
# per-market setting in the registry, and deliberately so: craigslist is the first marketplace whose
# posting destination is a choice rather than a fact about where the seller lives. Generalising it
# to a map over markets would be inventing a shape for one entry.
#
# Note the two meanings of "region" in play. `craigslist_areas` calls a US state `state`, because
# `region` here and everywhere else in sellee is an ISO *country* code — a "CA" means California in
# Craigslist's data and Canada in ours.


def _parse_craigslist_area(raw: object) -> str:
    """A craigslist area, stored as the abbreviation its posting flow requires.

    Accepts whatever a seller would plausibly name it — the abbreviation, the hostname from their
    own URL, the site's description, or a pasted area URL — and answers the abbreviation. Those are
    not interchangeable downstream: `post.craigslist.org/c/<hostname>` answers HTTP 200 and lands on
    a generic "choose area" picker, from which a listing goes wherever that picker decides, and a
    wrong area cannot be corrected after publishing.

    Empty clears it. Pure, per the settings contract: whether *this* seller may use the area is
    `_check_craigslist_area`.
    """
    if raw is None:
        return ""
    said = str(raw).strip()
    if not said:
        return ""
    try:
        area = craigslist_areas.resolve(said)
        craigslist_areas.check_servable(area)
    except (craigslist_areas.UnknownArea, craigslist_areas.AreaNotAvailable) as exc:
        raise SettingError(str(exc)) from exc
    return area


def _render_craigslist_area(value: object) -> str:
    if not value:
        return "not set"
    return craigslist_areas.display_name(str(value))


def _check_craigslist_area(value: object, store) -> None:
    """Refuse an area in a country the seller does not sell in.

    Redundant against today's single-area rollout — the parser already refuses everything outside
    it — and kept because it is the check that still holds when the rollout grows. Deliberately
    permissive about a *missing* region, exactly as `_check_connected_markets` is: a seller we have
    not asked yet is not told their area is wrong.
    """
    if not value:
        return
    area = craigslist_areas.get(str(value))
    region = store.seller_region()
    if area is None or not region or not area.get("country"):
        return
    if area["country"] != region:
        raise SettingError(
            f"{craigslist_areas.display_name(str(value))} is a "
            f"{area['country']} site and you sell in {region} — buyers there can't reach you"
        )


def craigslist_area(store) -> str:
    """Which craigslist site this seller's listings go on."""
    return str(get(store, "craigslist_area") or "")


def market_url_fields(market: str, store) -> dict:
    """Per-seller fields a market's URL templates need, for `marketplaces.market_url`.

    Craigslist's posting URL carries the seller's area and every other market's templates carry
    nothing, so this is empty for all of them. An unset area answers empty rather than a blank
    value: `market_url` formats unconditionally, so a missing field makes the URL None and the
    caller refuses up front — where a blank one would compose `.../c/` and be navigated to.
    """
    if market != "craigslist":
        return {}
    area = craigslist_area(store)
    return {"area": area} if area else {}


register(
    SettingSpec(
        key="craigslist_area",
        label="Craigslist city",
        parse=_parse_craigslist_area,
        render=_render_craigslist_area,
        default="sfo",
        description="Which Craigslist site your items go on. Craigslist is one city at a time and "
        "a post on the wrong one is invisible to your buyers, so this is not a preference — it "
        "decides who sees what you're selling. Right now I only work the SF bay area.",
        take_effect="applies to listings from now on; anything already posted stays where it is.",
        requires_approval=True,
    )
)


# The postal code craigslist's map step will not proceed without.
#
# Seller-level, not per-item: it places the posting on a map, and craigslist requires it before the
# wizard will advance. Nothing in an item record carries one, and a first live run proved what that
# costs — a publish pass walked the whole wizard correctly and then stopped at the details form
# because the recipe (rightly) refuses to invent a location for the seller. Forty-four model turns
# to learn something knowable at enqueue, which is why `validate_payload` now asks first.
#
# Craigslist-scoped like the area beside it. A general seller address belongs in seller_config, but
# nothing else needs one yet, and inventing that shape for one market's map step is speculative.

_POSTAL_CODE_LEN = 5


def _parse_craigslist_postal_code(raw: object) -> str:
    """A US ZIP, as craigslist's map step wants it. Empty clears it.

    Never guessed, never coerced: this decides where on a map the seller's item appears, so a value
    that is not a postal code is refused rather than trimmed into one. ZIP+4 is accepted and kept —
    craigslist takes it, and discarding the seller's own precision is not this parser's business.
    """
    if raw is None:
        return ""
    said = str(raw).strip()
    if not said:
        return ""
    head, _, tail = said.partition("-")
    ok = len(head) == _POSTAL_CODE_LEN and head.isdigit()
    if tail:
        ok = ok and len(tail) == 4 and tail.isdigit()
    if not ok:
        raise SettingError(
            f"{said!r} isn't a postal code — craigslist needs a 5-digit ZIP (or ZIP+4) to put your "
            "listing on its map"
        )
    return said


def craigslist_postal_code(store) -> str:
    """The ZIP craigslist's map step needs for this seller."""
    return str(get(store, "craigslist_postal_code") or "")


register(
    SettingSpec(
        key="craigslist_postal_code",
        label="Craigslist postal code",
        parse=_parse_craigslist_postal_code,
        render=lambda value: str(value) if value else "not set",
        default="",
        description="The ZIP code Craigslist puts your listing on the map with. It asks for one "
        "before it will take a posting, and I won't guess where you are — so without this I can't "
        "list there. It is shown on the posting as an area, not as your address.",
        take_effect="applies to listings from now on.",
        requires_approval=True,
    )
)


# The scoped view of the seller's mailbox that craigslist's buyer mail is read through.
#
# Craigslist buyers arrive as relay email, so answering them means reading a mailbox — and no mail
# provider offers a per-sender read scope, the narrowest read grant any of them has being the whole
# mailbox. So the limit is a *view*: a label the seller creates with their own filter, or a search.
# The transport navigates only that view, which makes the restriction structural rather than a
# promise about our code — it cannot read what the view does not contain.
#
# A label the seller made is preferred over a search because they can see it, audit what lands in
# it, and change it without asking us. Stored as the webmail URL of the view, because that is what
# the transport navigates and what the seller can check by opening it themselves.


def _parse_craigslist_mail_view(raw: object) -> str:
    """The webmail URL of the scoped view. Empty clears it.

    Refuses anything that is not an https URL: this value is navigated to in the seller's own
    signed-in browser, so a non-URL would be a navigation to nowhere and a non-https one would be
    a signed-in session over plaintext.
    """
    if raw is None:
        return ""
    said = str(raw).strip()
    if not said:
        return ""
    if not said.startswith("https://"):
        raise SettingError(
            "that needs to be the https:// address of the mail view holding your Craigslist "
            f"messages — a label or a saved search — not {said!r}"
        )
    return said


def _parse_craigslist_handoff_address(raw: object) -> str:
    """The +tagged address a buyer is invited to forward the thread to. Empty clears it.

    Refuses a plain address, and that refusal is the whole point rather than fussiness — see
    `sellee.mail.handoff`. Craigslist mints a fresh relay address per view of a posting and an
    expired one cannot be recovered, so a conversation that stays on the relay eventually goes
    unreachable, silently. The fix is to invite the buyer onto an address of the seller's own. But a
    *plain* address cannot be told apart from the rest of their mail, so scoping to it would put
    their whole mailbox in this agent's view. A +tag is what keeps the widening to one address that
    exists only for Craigslist.
    """
    from sellee.mail import handoff

    try:
        return handoff.parse(raw)
    except handoff.HandoffError as exc:
        raise SettingError(str(exc)) from exc


register(
    SettingSpec(
        key="craigslist_handoff_address",
        label="Craigslist forwarding address",
        parse=_parse_craigslist_handoff_address,
        render=lambda value: str(value) if value else "not set",
        default="",
        description="A +tagged version of your own email — something like you+cl@gmail.com — that "
        "I give to Craigslist buyers and ask them to forward the thread to. Craigslist's own "
        "forwarding address for an ad stops working after a while and can't be revived, so without "
        "this a buyer eventually can't be answered at all. The +tag matters: it means I watch one "
        "address that exists only for Craigslist rather than your whole inbox.",
        take_effect="applies to the next reply I send a Craigslist buyer.",
        requires_approval=True,
    )
)


register(
    SettingSpec(
        key="craigslist_mail_view",
        label="Craigslist mail view",
        parse=_parse_craigslist_mail_view,
        render=lambda value: str(value) if value else "not connected",
        default="",
        description="Where I look for your Craigslist buyer messages. Craigslist has no inbox of "
        "its own — buyers email you — so this is the address of a label or search in your own "
        "webmail that holds just those messages. I read only what that view shows me, so you "
        "decide what I can see and you can change it whenever you like.",
        take_effect="applies from the next time I check for messages.",
        requires_approval=True,
    )
)
