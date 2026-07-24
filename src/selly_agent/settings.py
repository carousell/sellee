"""The runtime-settings registry: the one place a seller-facing behavior knob is defined.

A setting is distinct from the two other config homes. seller_config holds domain records
(basics, shipping zones, the origin address) — data the flows consult. The operator config file
holds install/operator knobs with restart semantics. A *setting* is a behavior knob the seller
changes at runtime, through a door, under a change protocol: the LLM may only propose, and every
apply happens in deterministic daemon code behind a human signal.

Each key's schema lives here as a SettingSpec — its parser/validator, human renderer, default,
description, take-effect note, and whether a change needs approval. The registry is at once the
validation source, the discoverability source (the /selly card and get_settings read it), and the
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

log = logging.getLogger(__name__)

# An unaccepted proposal expires after this long — long enough that an approval notice sitting on a
# phone is still tappable hours later, short enough that a stale window doesn't linger for days.
PROPOSAL_TTL_SEC = 24 * 3600

# Settings always shown on the /selly card even at their default — the discoverable headline set.
# Everything else appears on the card only once changed from its default (the card scales with
# customization, not catalog size).
CARD_HEADLINE = ("quiet_hours",)

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


# --- discoverability renderers (F10) ---------------------------------------------------------


def card_lines(store) -> list[str]:
    """The /selly card's settings lines: every changed-from-default setting plus the headline set,
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
