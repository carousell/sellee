"""propose_setting_change and get_settings — the LLM's *propose-only* settings surface.

The model can read every setting (get_settings) and propose a change (propose_setting_change); it
can never apply, approve, or undo one. propose validates and canonicalizes the raw value against the
registry, then the daemon — not the model — decides by policy: a change that requires approval is
held (a pending row + an approval notice through a door); anything else applies immediately in
deterministic store code. The tool returns the canonical value, its human rendering, and the
decision. There is deliberately no apply/approve/undo tool on any tier — that guarantee is the
whole point of the split (the legacy learned the hard way that trusting the model to write the
approvals table corrupted configs).
"""

from __future__ import annotations

import json

from selly_agent import settings
from selly_agent.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)


def _decode_raw(raw: object) -> object:
    """A setting's value shape is per-key, so `raw_value` has no declared schema type — and an MCP
    client (Claude) commonly sends an untyped structured value as a JSON-encoded string ("[23, 9]"
    for a list). Decode that here so the registry parser always sees the real structure; a value
    that isn't JSON (a bare word for a text setting) passes through unchanged."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return raw
    return raw


def _propose_setting_change(ctx: ToolContext, params: dict) -> dict:
    key = params["key"]
    spec = settings.get_spec(key)
    if spec is None:
        raise ToolError(f"unknown setting {key!r} — call get_settings for the list of settings")
    try:
        value = spec.parse(_decode_raw(params["raw_value"]))
    except settings.SettingError as exc:
        raise ToolError(str(exc)) from exc

    current = settings.get(ctx.store, key)
    rendered = spec.render(value)
    # Proposing the current value is a no-op — no proposal row, nothing to approve.
    if settings.canonical_json(value) == settings.canonical_json(current):
        return {"status": "unchanged", "key": key, "value": value, "rendered": rendered}

    change_id = ctx.store.new_change_id()
    if spec.requires_approval:
        text, controls = settings.approval_notice(spec, change_id, value, current)
        ctx.store.propose_setting_change(
            key,
            value,
            change_id=change_id,
            prior_value=current,
            notice_text=text,
            notice_controls=controls,
        )
        ctx.bus.publish(
            "setting.proposed",
            {
                "change_id": change_id,
                "key": key,
                "value": value,
                "rendered": rendered,
                "requires_approval": True,
            },
            pass_id=ctx.session.pass_id,
        )
        return {
            "status": "held",
            "change_id": change_id,
            "key": key,
            "value": value,
            "rendered": rendered,
            "requires_approval": True,
            "take_effect": spec.take_effect,
            "message": f"Proposed {spec.label.lower()} → {rendered}. Waiting for the seller to "
            "approve it (they'll see Approve/Cancel).",
        }

    # No approval needed: apply now, in one store transaction, with an echo notice carrying Undo.
    text, controls = settings.echo_notice(spec, change_id, value, current)
    ctx.store.apply_setting_now(
        key,
        value,
        change_id=change_id,
        prior_value=current,
        decided_via="auto",
        notice_text=text,
        notice_controls=controls,
    )
    ctx.bus.publish(
        "setting.proposed",
        {
            "change_id": change_id,
            "key": key,
            "value": value,
            "rendered": rendered,
            "requires_approval": False,
        },
        pass_id=ctx.session.pass_id,
    )
    settings.publish_changed(ctx.bus, spec, change_id, value, current)
    return {
        "status": "applied",
        "change_id": change_id,
        "key": key,
        "value": value,
        "rendered": rendered,
        "requires_approval": False,
        "take_effect": spec.take_effect,
        "message": f"Set {spec.label.lower()} to {rendered}. {spec.take_effect}",
    }


def _get_settings(ctx: ToolContext, params: dict) -> dict:
    """Every registered setting with its value, default, description, and approval policy — the
    tail-discovery source the card doesn't inline."""
    return {"settings": settings.describe(ctx.store)}


register(
    ToolSpec(
        name="propose_setting_change",
        description="Propose a change to a seller setting (you can only propose — the seller "
        "approves high-stakes changes; low-stakes ones apply immediately). Pass the setting key "
        "and the new value in the setting's own shape (e.g. quiet_hours as [start, end] times — "
        '["23:00", "09:30"] for HH:MM, or [23, 9] for whole hours). Returns the canonical value, '
        "its rendering, and whether it was applied or is held for approval. Call get_settings for "
        "the keys and their shapes.",
        input_schema={
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                # No type constraint: the value's shape is the setting's own, validated by the
                # registry parser (which returns a caller-facing error the model round-trips).
                "raw_value": {},
            },
            "required": ["key", "raw_value"],
            "additionalProperties": False,
        },
        handler=_propose_setting_change,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_CHANNEL}),
    )
)
register(
    ToolSpec(
        name="get_settings",
        description="List every seller setting with its current value, default, description, and "
        "whether changing it needs the seller's approval. Use this to answer 'what can I change?' "
        "and to see a setting's exact shape before proposing a change.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_get_settings,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_CHANNEL}),
    )
)
