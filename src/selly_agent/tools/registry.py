"""The typed tool registry and the one dispatch path every tool call flows through.

A ToolSpec is declarative data: name, description, a JSON-Schema-subset input schema, the names
of its secret parameters, the session tiers allowed to see and call it, and a handler. Tools
register themselves at import time; nothing mutates the registry at runtime.

Dispatch is a single choke point so the load-bearing rules hold for every tool uniformly:
  * tier filtering gates both tools/list and tools/call — a tool a session's tier can't see is
    indistinguishable from one that doesn't exist (a hidden tool that still executed would be no
    tier at all);
  * secret parameters are masked in a copied payload before anything reaches the event bus, so a
    marked value never lands in a sink;
  * every call publishes tool.call then tool.result or tool.error, keyed by the session's pass id
    — the server-side ground truth against model narration.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Callable

from .schema import ValidationError, validate

_MASK = "***"
_RESULT_EVENT_CAP = 4096  # chars of a result's JSON kept in the event payload (not the return)


@dataclass(frozen=True)
class Session:
    tier: str
    pass_id: str | None = None
    # None = full scope (attended sessions, and the publish pass that touches only its own item).
    # A Scope (selly_agent.store.Scope) binds a headless pass to the entities it was spawned for;
    # the ScopedStore built for the request enforces it at every row load.
    scope: object = None


@dataclass
class ToolContext:
    """Everything a handler is allowed to touch. Built per request by the server."""

    session: Session
    store: object  # selly_agent.store.Store
    bus: object  # selly_agent.events.EventBus
    config: object  # selly_agent.config.Config
    # A factory so the publish tool can surface an "unprovisioned" error itself rather than the
    # server failing to build a rail client up front. May be None where no tool needs a rail.
    rail_factory: Callable[[], object] | None = None
    # The marketplace reply sink (a daemon-owned browser send in a later plan). None here: 04 ships
    # no live sink, so send_reply returns a structured no_send_path for real markets.
    reply_sink: object | None = None
    started_ts: float = 0.0


Handler = Callable[[ToolContext, dict], dict]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Handler
    tiers: frozenset = frozenset()
    secret_params: frozenset = frozenset()


class ToolError(Exception):
    """A tool-level failure surfaced to the caller as an isError result — never a transport error.

    Its message is caller-facing and must never carry a secret value.
    """


class UnknownTool(Exception):
    """The named tool does not exist for this session (truly absent or hidden by tier)."""


_REGISTRY: dict = {}


def register(spec: ToolSpec) -> ToolSpec:
    if spec.name in _REGISTRY:
        raise ValueError(f"duplicate tool registration: {spec.name}")
    _REGISTRY[spec.name] = spec
    return spec


def all_specs() -> list:
    """Every registered spec, in registration order (drives inventory/tools-list ordering)."""
    return list(_REGISTRY.values())


def tools_for_tier(tier: str) -> list:
    return [spec for spec in _REGISTRY.values() if tier in spec.tiers]


def _mask_params(params: dict, secret_params) -> dict:
    masked = copy.deepcopy(params)
    for name in secret_params:
        if name in masked:
            masked[name] = _MASK
    return masked


def _capped(result: object) -> object:
    """A size-bounded view of a result for the event payload — the transcript store is an
    observability record, not a verbatim log. The full result is still returned to the caller."""
    encoded = json.dumps(result, separators=(",", ":"), sort_keys=True, default=str)
    if len(encoded) <= _RESULT_EVENT_CAP:
        return result
    return {"_truncated": True, "preview": encoded[:_RESULT_EVENT_CAP]}


def _resolve_for_session(name: str, session: Session) -> ToolSpec:
    spec = _REGISTRY.get(name)
    if spec is None or session.tier not in spec.tiers:
        # Same error whichever it is: a tier-hidden tool must not leak its existence.
        raise UnknownTool(f"unknown tool: {name}")
    return spec


def dispatch(name: str, params: dict, ctx: ToolContext) -> dict:
    """Validate, mask, log, run, log — the one path every tool call takes.

    Raises UnknownTool for an absent/hidden tool and ToolError for any tool-level failure; the
    server maps both onto isError results. On success returns the handler's raw result.
    """
    spec = _resolve_for_session(name, ctx.session)
    if not isinstance(params, dict):
        raise ToolError("tool arguments must be an object")

    masked = _mask_params(params, spec.secret_params)
    try:
        validate(spec.input_schema, params)
    except ValidationError as exc:
        ctx.bus.publish(
            "tool.error",
            {"tool": name, "params": masked, "error": str(exc)},
            pass_id=ctx.session.pass_id,
        )
        raise ToolError(str(exc)) from exc

    ctx.bus.publish(
        "tool.call",
        {"tool": name, "params": masked, "tier": ctx.session.tier},
        pass_id=ctx.session.pass_id,
    )
    try:
        result = spec.handler(ctx, params)
    except ToolError as exc:
        ctx.bus.publish(
            "tool.error", {"tool": name, "error": str(exc)}, pass_id=ctx.session.pass_id
        )
        raise
    except Exception as exc:  # a handler bug must not leak internals to the caller
        ctx.bus.publish(
            "tool.error", {"tool": name, "error": "internal error"}, pass_id=ctx.session.pass_id
        )
        raise ToolError("internal error") from exc

    ctx.bus.publish(
        "tool.result", {"tool": name, "result": _capped(result)}, pass_id=ctx.session.pass_id
    )
    return result


# Tier labels. Open strings; later plans add tiers without registry surgery.
TIER_ATTENDED = "attended"
TIER_PASS_PUBLISH = "pass:publish"
# Provisional: the reply-loop tool subset carries this tier so entity-scope enforcement is
# exercisable end to end by tests. No reply pass *type* exists yet — the skills rewrite finalizes
# pass-tier membership; this label only makes the scoped-token path testable now.
TIER_PASS_REPLY = "pass:reply"
