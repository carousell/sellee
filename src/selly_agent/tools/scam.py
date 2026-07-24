"""Scam-guard tools: scan an inbound message, and record/retract a local signature.

scam_scan composes the pure engine with store-backed state: it builds the marketplace allowlist
from the shipped registry, reads the merged signature view (packaged registry ∪ local bank), pulls
the thread's recent inbound window from the transcript, and passes the config-derived checkout base
so the carve-out can never drift. record refuses to bank a real marketplace host as a scam
signature (anti-poisoning), in code.
"""

from __future__ import annotations

from selly_agent import marketplaces
from selly_agent.engines import hosts
from selly_agent.engines import scam as scam_engine
from selly_agent.store import StoreError
from selly_agent.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_REPLY,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)

_HISTORY_WINDOW = 10  # inbound messages of context when a thread is given
# The skill/CLI vocabulary maps to the bank's canonical kinds.
_RECORD_KIND_MAP = {
    "link": "domain",
    "domain": "domain",
    "pattern": "message_pattern",
    "message_pattern": "message_pattern",
}


def _checkout_base(ctx: ToolContext) -> str:
    return ctx.config.carousell_ai_api_base.rstrip("/") + "/checkout"


def _allowlist():
    return hosts.build_allowlist(marketplaces.all_marketplaces())


def _scam_scan(ctx: ToolContext, params: dict) -> dict:
    thread_id = params["thread_id"]
    history = ""
    messages = ctx.store.get_thread_messages(thread_id, limit=None)
    inbound = [m["text"] for m in messages if m["dir"] == "in"]
    if inbound:
        history = "\n".join(inbound[-_HISTORY_WINDOW:])
    merged, registry_ok = ctx.store.merged_scam_signatures()
    return scam_engine.scan(
        params["text"],
        history_text=history,
        allowlist=_allowlist(),
        signatures=merged,
        checkout_base=_checkout_base(ctx),
        registry_ok=registry_ok,
    )


def _record_scam_signature(ctx: ToolContext, params: dict) -> dict:
    kind = params["kind"]
    canonical = _RECORD_KIND_MAP.get(kind)
    if canonical is None:
        raise ToolError(f"unknown kind {kind!r} (use link|pattern)")
    value = params["value"]
    if canonical == "domain":
        host = scam_engine.normalize("domain", value)
        if host and hosts.host_is_marketplace(host, _allowlist()):
            raise ToolError(f"refusing to bank a real marketplace host as a scam signature: {host}")
    try:
        return ctx.store.add_scam_signature(
            kind=canonical,
            value=value,
            marketplace=params["market"],
            thread_id=params["thread_id"],
            context=params["context"],
            severity=params.get("severity", "medium"),
            detected_by=params.get("source", "detect"),
        )
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def _retract_scam_signature(ctx: ToolContext, params: dict) -> dict:
    removed = ctx.store.retract_detect_scam(params["thread_id"])
    return {"removed": removed}


register(
    ToolSpec(
        name="scam_scan",
        description="Score an inbound marketplace message for scam signals (off-platform links, "
        "signature hits, delivery+payment-link combos). Advisory; never blocks a reply on its own.",
        input_schema={
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "side": {"type": "string", "enum": ["sell", "buy"]},
                "market": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["thread_id", "market", "text"],
            "additionalProperties": False,
        },
        handler=_scam_scan,
        tiers=frozenset({TIER_PASS_CHANNEL, TIER_ATTENDED, TIER_PASS_REPLY}),
    )
)
register(
    ToolSpec(
        name="record_scam_signature",
        description="Add a scam signature to this install's local bank. Refuses a real marketplace "
        "host. kind is link (a domain) or pattern (message text).",
        input_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["link", "pattern"]},
                "value": {"type": "string"},
                "market": {"type": "string"},
                "thread_id": {"type": "string"},
                "context": {"type": "string"},
                "source": {"type": "string", "enum": ["detect", "seller_confirm"]},
                "severity": {"type": "string", "enum": ["low", "medium", "high"]},
            },
            "required": ["kind", "value", "market", "thread_id", "context"],
            "additionalProperties": False,
        },
        handler=_record_scam_signature,
        tiers=frozenset({TIER_ATTENDED}),
    )
)
register(
    ToolSpec(
        name="retract_scam_signature",
        description="Retract the detect-sourced scam signatures recorded for a thread "
        "(a false-positive undo). Seller-confirmed and registry signatures are never dropped.",
        input_schema={
            "type": "object",
            "properties": {"thread_id": {"type": "string"}},
            "required": ["thread_id"],
            "additionalProperties": False,
        },
        handler=_retract_scam_signature,
        tiers=frozenset({TIER_ATTENDED}),
    )
)
