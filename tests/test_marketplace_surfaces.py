"""What connecting a marketplace promises, held to the code that implements it.

Connecting is one promise, not a menu. A seller who switches a marketplace on is told Sellee will
list to it, read its inbox, answer its buyers, and pick up what they already have listed there — so
a marketplace that arrives with three of those and nothing to say about the fourth is a broken
promise nobody notices. Facebook arrived exactly that way: an adapter and a registry entry, offered
at onboarding, with no listings survey, no publish path, and an inbox reader pointed at a page that
carries no thread identity. Every one of those was a separate afternoon of finding out.

So each surface is derived here from the artifact, registry template or skill that implements it —
never from a flag, which could say yes while the adapter says no.

The waivers below are **subtractive**: a `(market, surface)` pair may declare that a gap is known
and why. They can only ever say *less* than the code does, so a waiver cannot claim a capability
that is absent, and it cannot hide one that arrives — closing a gap fails this test until the
waiver is deleted. Same shape as `NETWORK_ALLOWLIST` and `ALLOWED_RUNTIME_DEPS`.
"""

from __future__ import annotations

import pytest

from sellee import marketplaces
from sellee.browser import markets as market_adapters

# A gap someone has looked at and decided to ship without, with the reason. Delete the entry when
# the gap closes — this test fails while a waiver describes a surface that now works.
#
# Empty today, and it got there the way it is supposed to: Facebook shipped with a `publish` waiver,
# the driver landed, and the self-retirement test below failed until the waiver was deleted.
WAIVERS: dict = {}


def _a_region_it_serves(market: str) -> str | None:
    """A region this marketplace actually operates in, taken from its own registry entry.

    Offerability is a question about a *seller*, so it needs a region: asking about Carousell with
    none reports it unofferable, which is true of that seller and says nothing about whether the
    marketplace is wired up. Derived rather than tabulated so the next marketplace needs no edit
    here — a table would be one more thing to forget, which is the failure this file exists for.
    """
    domains = (marketplaces.get_marketplace(market) or {}).get("domains") or {}
    for region in domains:
        return None if region == "*" else region
    return None


def _surfaces(market: str) -> dict:
    """What the code actually provides for one marketplace, surface by surface.

    Every value is read off the thing that does the work. `connect` is deliberately absent as a
    separate entry: it needs nothing beyond an adapter and a registry entry, so it is exactly
    `offer` and would be a second name for one fact.
    """
    adapter = market_adapters.get_adapter(market)
    if adapter is None:
        return {}
    urls = marketplaces.urls(market)
    return {
        # 1 + 2 — offered at onboarding, and switchable on the /sellee card.
        "offer": market in market_adapters.connectable_markets(_a_region_it_serves(market)),
        # 3 — listing to it: a recipe the model follows, or selectors a driver fills. Asked of
        # `supported_markets` rather than restated, so the guard cannot drift from the reader every
        # other caller uses.
        "publish": market in market_adapters.supported_markets(),
        # 4 — picking up what the seller already has listed there.
        "adopt": bool(
            adapter.my_listings_js and adapter.listing_detail_js and urls.get("my_listings")
        ),
        # 5 — reading the inbox and answering buyers.
        "inbox": bool(
            adapter.conversations_list_js
            and adapter.conversation_tail_js
            and adapter.listing_id_pattern
            and adapter.composer_step(market_adapters.MESSAGE_BOX)
            and urls.get("inbox")
            and urls.get("thread")
        ),
        # 6 — getting back in when the session drops. Structurally guaranteed: a field with no
        # default, so an adapter cannot be constructed without one.
        "signin": bool(adapter.login_js),
    }


def _markets():
    return market_adapters.drivable_markets()


def test_there_is_at_least_one_marketplace_to_check() -> None:
    """Guards the guard: an empty registry would make every assertion below vacuous."""
    assert _markets()


@pytest.mark.parametrize("market", market_adapters.drivable_markets())
def test_every_connected_marketplace_keeps_the_whole_promise(market) -> None:
    """One case per marketplace, so a failure names which one fell short and on what."""
    provided = _surfaces(market)
    missing = sorted(name for name, ok in provided.items() if not ok)
    unwaived = [name for name in missing if (market, name) not in WAIVERS]

    assert not unwaived, (
        f"{market} is offered as a connection but does not deliver {unwaived}. "
        "Either implement the surface, or add a (market, surface) waiver saying why not."
    )


@pytest.mark.parametrize("market,surface", sorted(WAIVERS))
def test_a_waiver_retires_itself_once_the_gap_closes(market, surface) -> None:
    """The half that makes a waiver safe. Without this a waiver written during a gap would sit
    there forever, quietly excusing a surface that had worked for months."""
    provided = _surfaces(market)
    assert provided, f"waiver names {market!r}, which has no adapter"
    assert surface in provided, f"waiver names an unknown surface {surface!r}"
    assert not provided[surface], (
        f"{market} now delivers {surface!r} — delete its waiver in {__name__}."
    )


def test_signin_cannot_be_waived_away() -> None:
    """The one surface with no legitimate gap: `login_js` has no default, so an adapter cannot be
    built without it, and a market nobody can sign back in to is dead the first time it logs out."""
    assert not [pair for pair in WAIVERS if pair[1] == "signin"]
    for market in _markets():
        assert _surfaces(market)["signin"]
