"""The market-adapter seam: everything a marketplace does differently, in one module each.

An adapter carries the per-market facts — where the inbox is, how to pull thread links off it,
how to read a chat's tail, where the composer is, how to tell whether the seller is logged in,
and which publish recipe to load. The generic layer above (client, inbox lane, reconcile, reply
sink, publish plumbing) knows only this protocol, so a new marketplace is a new module plus a
registry entry, not edits threaded through the layer — the same split `channel/` uses for providers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from selly_agent.browser.markets import carousell


@dataclass(frozen=True)
class Selector:
    """A shipped selector default: known-good at release, and the fallback under the heal cache.

    Selectors ship as code so a fresh install pays no vision cost to find a composer, and heal into
    the cache when a marketplace moves one — so self-healing never waits on a release, and a release
    refreshes the defaults underneath whatever the cache has learned.
    """

    step: str
    strategy: str
    query: str
    action_kind: str
    page_url_pattern: str


@dataclass(frozen=True)
class MarketAdapter:
    """One marketplace's browser contract."""

    market: str
    # JS artifacts, each a function expression for browser_evaluate.
    discovery_js: str
    tail_js: str
    login_js: str
    # The reply composer's shipped selector defaults, by step.
    composer: tuple = ()
    # The skill holding this market's publish recipe.
    publish_skill: str = ""
    # Rows an inbox read should never treat as a buyer conversation.
    system_handles: frozenset = field(default_factory=frozenset)

    def composer_step(self, step: str) -> Selector | None:
        for selector in self.composer:
            if selector.step == step:
                return selector
        return None


CAROUSELL = MarketAdapter(
    market="carousell",
    discovery_js=carousell.DISCOVERY_JS,
    tail_js=carousell.TAIL_JS,
    login_js=carousell.LOGIN_JS,
    composer=tuple(Selector(**row) for row in carousell.COMPOSER_DEFAULTS),
    publish_skill=carousell.PUBLISH_SKILL,
    system_handles=carousell.SYSTEM_HANDLES,
)

_ADAPTERS = {CAROUSELL.market: CAROUSELL}

# The flow name the composer selectors are cached under.
REPLY_FLOW = "reply"


def get_adapter(market: str) -> MarketAdapter | None:
    return _ADAPTERS.get(market)


def adapters() -> list:
    return list(_ADAPTERS.values())
