"""Finding a control on a marketplace page: shipped defaults, a heal cache over them, one rule.

Selectors ship as code because a fresh install should not pay a vision round-trip to find a message
box that was known-good at release. The cache sits *over* those defaults as a heal overlay: when a
marketplace moves a control, whatever re-found it is recorded and used from then on, and a later
release refreshes the defaults underneath. So self-healing never waits on a release, and a release
never overwrites what the install has learned.

The one rule is that a selector only ever tells you *where* to look. Resolving is locate-only, on
the right page, and must match exactly one visible element — zero or several is a miss, and a miss
means the caller falls back or fails closed, never "act on the first one".
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Resolved:
    """A selector chosen for one step, and where it came from."""

    step: str
    strategy: str
    query: str
    target: str
    page_url_pattern: str
    source: str  # "cache" | "shipped"


def as_target(strategy: str, query: str) -> str | None:
    """The CSS target an input verb can be pointed at, or None when the strategy cannot give one."""
    if strategy == "css":
        return query
    if strategy == "aria":
        return f'[aria-label="{query}"]'
    if strategy == "role":
        return f'[role="{query}"]'
    return None


def _resolved(step: str, strategy: str, query: str, pattern: str, source: str) -> Resolved | None:
    target = as_target(strategy, query)
    if target is None:
        return None
    return Resolved(
        step=step,
        strategy=strategy,
        query=query,
        target=target,
        page_url_pattern=pattern,
        source=source,
    )


def candidates(store, market: str, flow: str, step: str, shipped) -> list:
    """What to try for a step, best first: a live cache row, then the shipped default.

    A stale row is skipped rather than tried — it has failed too often, has no page guard, or has
    gone unverified too long, so the shipped default deserves its turn ahead of a selector we have
    reason to doubt already. Trying both is what makes the cache an accelerator: a heal that has
    since gone bad costs one extra locate, not a failed send.
    """
    out = []
    entry = store.ui_cache_get(market, flow, step).get("selector")
    if entry and not entry["stale"]:
        cached = _resolved(
            step, entry["strategy"], entry["query"], entry["page_url_pattern"], "cache"
        )
        if cached is not None:
            out.append(cached)
    if shipped is not None:
        default = _resolved(
            step, shipped.strategy, shipped.query, shipped.page_url_pattern, "shipped"
        )
        if default is not None and not any(c.target == default.target for c in out):
            out.append(default)
    return out


def locate_js(target: str) -> str:
    """A locate-only probe: how many visible elements this target matches, and where we are.

    Deliberately read-only. Resolving a control this way is fine; setting a value this way is not —
    synthetic input carries none of the focus and keystroke cadence of a real one.
    """
    encoded = json.dumps(target)
    return (
        "() => {\n"
        f"  const nodes = Array.from(document.querySelectorAll({encoded}));\n"
        "  const visible = nodes.filter((n) => {\n"
        "    const r = n.getBoundingClientRect();\n"
        "    return r.width > 0 && r.height > 0;\n"
        "  });\n"
        "  return { matches: visible.length, url: location.href };\n"
        "}"
    )


def probe(client, target: str) -> dict:
    """Run the locate probe and return `{matches, url}`."""
    result = client.evaluate(locate_js(target))
    if not isinstance(result, dict):
        return {"matches": 0, "url": ""}
    return {"matches": int(result.get("matches") or 0), "url": str(result.get("url") or "")}


def usable(result: dict, page_url_pattern: str) -> bool:
    """Whether a probe's answer is one we may act on: the right page, and exactly one element.

    Several matches is a miss on purpose — picking one of them is a guess, and the guess would be
    made once per send, silently, on the account-sensitive path.
    """
    if page_url_pattern and page_url_pattern not in result.get("url", ""):
        return False
    return result.get("matches") == 1
