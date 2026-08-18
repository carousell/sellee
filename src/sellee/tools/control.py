"""pause_agent / resume_agent — the attended control tools over the pause flag.

The deterministic /pause and /resume channel fast paths flip the same flag daemon-side; these are
the attended-session equivalents (a /sell or /sellee session on the same MCP server). Pausing is
absolute: a paused daemon runs but acts on nothing (the pass lane claims nothing, send-capable
tools refuse, the babysitter kills a running pass within a poll cadence). Missing row = not paused.
"""

from __future__ import annotations

from sellee.tools.registry import TIER_ATTENDED, ToolContext, ToolSpec, register


def _pause_agent(ctx: ToolContext, params: dict) -> dict:
    ctx.store.set_paused(True, source=params.get("source", "attended"))
    ctx.bus.publish("agent.paused", {"source": params.get("source", "attended")})
    return {"paused": True}


def _resume_agent(ctx: ToolContext, params: dict) -> dict:
    ctx.store.set_paused(False, source=params.get("source", "attended"))
    ctx.bus.publish("agent.resumed", {"source": params.get("source", "attended")})
    return {"paused": False}


_SOURCE_SCHEMA = {
    "type": "object",
    "properties": {"source": {"type": "string"}},
    "additionalProperties": False,
}

register(
    ToolSpec(
        name="pause_agent",
        description="Pause the agent: it keeps running but acts on nothing (no pass claims, sends "
        "refuse, a running pass is stopped) until resumed.",
        input_schema=_SOURCE_SCHEMA,
        handler=_pause_agent,
        tiers=frozenset({TIER_ATTENDED}),
    )
)
register(
    ToolSpec(
        name="resume_agent",
        description="Resume the agent after a pause — pending work routes again on the next tick.",
        input_schema=_SOURCE_SCHEMA,
        handler=_resume_agent,
        tiers=frozenset({TIER_ATTENDED}),
    )
)
