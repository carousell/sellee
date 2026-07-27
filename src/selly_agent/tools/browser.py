"""The selector-cache tools: read what we know, probe a page, record what was found.

These are the model's half of the heal ladder. When a marketplace moves a control, the pass that hit
the miss can re-find it by looking at the page, confirm the new selector with a read-only probe, and
record it — so the next send runs from the cache instead of paying for vision again.

`probe_selector` is executed by the daemon on its own browser client, not by the pass: it exists so
a flow can confirm a selector resolves without being handed browser control. The probe is
locate-only — it counts visible matches and reports the URL, and cannot click or type.

None of this is on the reply pass's tier. A reply pass processes untrusted buyer text, and a buyer
who could get a selector recorded would be choosing where the agent's next reply gets typed; there,
a selector miss stays a fail-closed send and a needs-me notice.
"""

from __future__ import annotations

from selly_agent.browser import markets as market_adapters
from selly_agent.browser import selectors
from selly_agent.browser.client import BrowserError
from selly_agent.store import StoreError
from selly_agent.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_PUBLISH,
    ToolContext,
    ToolError,
    ToolSpec,
    register,
)

_STRATEGIES = ("css", "aria", "role", "text")
_ACTION_KINDS = ("type", "click", "upload", "select")


def _client(ctx: ToolContext):
    if ctx.browser_factory is None:
        raise ToolError("the browser is not available in this session")
    try:
        return ctx.browser_factory()
    except BrowserError as exc:
        raise ToolError(str(exc)) from exc


def _ui_cache_get(ctx: ToolContext, params: dict) -> dict:
    return ctx.store.ui_cache_get(params["market"], params["flow"], params.get("step"))


def _ui_cache_record(ctx: ToolContext, params: dict) -> dict:
    try:
        return ctx.store.ui_cache_record(
            market=params["market"],
            flow=params["flow"],
            step=params["step"],
            strategy=params["strategy"],
            query=params["query"],
            page_url_pattern=params["page_url_pattern"],
            action_kind=params.get("action_kind", ""),
        )
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def _ui_cache_invalidate(ctx: ToolContext, params: dict) -> dict:
    return ctx.store.ui_cache_invalidate(params["market"], params["flow"], params.get("step"))


def _probe_selector(ctx: ToolContext, params: dict) -> dict:
    """Resolve a selector on whatever page the daemon's tab is on, and report what it found."""
    strategy = params.get("strategy", "css")
    target = selectors.as_target(strategy, params["selector"])
    if target is None:
        raise ToolError(
            f"a {strategy!r} selector cannot be resolved to an element to act on — "
            "record a css, aria, or role selector instead"
        )
    if market_adapters.get_adapter(params["market"]) is None:
        raise ToolError(f"no browser adapter for market {params['market']!r}")
    client = _client(ctx)
    try:
        result = selectors.probe(client, target)
    except BrowserError as exc:
        raise ToolError(str(exc)) from exc
    # Exactly one visible match is the only usable answer: none means it is not there, and several
    # means acting would be a guess.
    return {
        "market": params["market"],
        "selector": params["selector"],
        "matches": result["matches"],
        "url": result["url"],
        "usable": result["matches"] == 1,
    }


register(
    ToolSpec(
        name="ui_cache_get",
        description="Read the remembered selectors for a market's flow (one step, or the whole "
        "flow). A stale entry is flagged and must be treated as a miss.",
        input_schema={
            "type": "object",
            "properties": {
                "market": {"type": "string"},
                "flow": {"type": "string"},
                "step": {"type": "string"},
            },
            "required": ["market", "flow"],
            "additionalProperties": False,
        },
        handler=_ui_cache_get,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_PUBLISH}),
    )
)
register(
    ToolSpec(
        name="ui_cache_record",
        description="Remember where a control was found, after confirming it resolves. Requires a "
        "page_url_pattern (a selector with no page guard is never trusted). Never record a value, "
        "price, or address — only how to locate an element.",
        input_schema={
            "type": "object",
            "properties": {
                "market": {"type": "string"},
                "flow": {"type": "string"},
                "step": {"type": "string"},
                "strategy": {"type": "string", "enum": list(_STRATEGIES)},
                "query": {"type": "string"},
                "page_url_pattern": {"type": "string"},
                "action_kind": {"type": "string", "enum": list(_ACTION_KINDS)},
            },
            "required": ["market", "flow", "step", "strategy", "query", "page_url_pattern"],
            "additionalProperties": False,
        },
        handler=_ui_cache_record,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_PUBLISH}),
    )
)
register(
    ToolSpec(
        name="ui_cache_invalidate",
        description="Forget a remembered selector (one step, or a whole flow) after it failed to "
        "resolve or acted on the wrong thing.",
        input_schema={
            "type": "object",
            "properties": {
                "market": {"type": "string"},
                "flow": {"type": "string"},
                "step": {"type": "string"},
            },
            "required": ["market", "flow"],
            "additionalProperties": False,
        },
        handler=_ui_cache_invalidate,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_PUBLISH}),
    )
)
register(
    ToolSpec(
        name="probe_selector",
        description="Check whether a selector resolves to exactly one visible element on the page "
        "the agent's browser is on. Read-only: it counts matches and reports the URL, and cannot "
        "click, type, or read content.",
        input_schema={
            "type": "object",
            "properties": {
                "market": {"type": "string"},
                "selector": {"type": "string"},
                "strategy": {"type": "string", "enum": list(_STRATEGIES)},
            },
            "required": ["market", "selector"],
            "additionalProperties": False,
        },
        handler=_probe_selector,
        tiers=frozenset({TIER_ATTENDED, TIER_PASS_PUBLISH}),
    )
)
