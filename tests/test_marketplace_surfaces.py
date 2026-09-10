"""What connecting a marketplace promises, held to the code that implements it.

Connecting is one promise: a seller who switches a marketplace on is told Sellee will list to it,
read its inbox, answer its buyers, and pick up what they already have listed there. Each surface
is derived from the artifact, registry template or skill that implements it — never from a flag,
which could say yes while the adapter says no.

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
WAIVERS: dict = {
    ("craigslist", "answers_buyers"): (
        "Craigslist's relay carries a buyer's message to the seller and has never been observed "
        "carrying a reply back. Measured 2026-09-10 on one live posting: inbound worked 3 times "
        "out of 3; outbound failed 4 times — twice to `<hex>@reply.craigslist.org`, once to "
        "`<hex>@sale.craigslist.org`, and once from a **hand-typed** reply with none of this code "
        "in the path. No bounce was produced by any of them, while a *stale* hex did bounce 550 "
        '"get a current reply email address" — so the relay is not a blind catch-all: it has '
        "routing state and chose to accept and discard the live ones.\n\n"
        "Nothing craigslist documents predicts this. Their help page says contact information "
        '"passes through unaltered" and that threads "continue for up to 4 months"; their '
        "relay-error page lists no silent-drop case; and both relay domains resolve the same MX, "
        "so it is not a send-only domain.\n\n"
        "**So the promise is withdrawn rather than kept badly.** What the transport does deliver "
        "is real: it reads the buyer and passes their words to the seller in chat, instead of the "
        "seller watching an inbox. `market_adapters.READ_ONLY_BUYERS` is where this lives, and it "
        "is a capability flag with the evidence attached — if a clean account is later observed "
        "replying successfully, delete the entry and this waiver with it."
    ),
    ("craigslist", "inbox"): (
        "Craigslist has no on-site messaging to read or reply in: a buyer's only route to a "
        "seller is an anonymised relay email, and the account page shows no replies at all. So "
        "this gap is not work waiting to be done — the surface does not exist to implement, and "
        "the adapter's conversation artifacts say so rather than pretending.\n\n"
        "**Its buyers are answered anyway**, by the mail transport in `sellee/mail/`: a scoped "
        "read of the mailbox they email, and a reply sent from it. That is graded by the "
        "`answers_buyers` surface — which is *also* waived here, for a different and measured "
        "reason (see that entry). This entry waives only the *browser* mechanism. Both exist "
        "because grading one for the other is what made a delivered surface read as a permanent "
        "gap; that reasoning still holds, and reading buyers is still delivered."
    ),
}


def _a_region_it_serves(market: str) -> str | None:
    """A region this marketplace operates in, from its own registry entry.

    Offerability is a question about a seller, so it needs a region. Derived rather than tabulated
    so the next marketplace needs no edit here.
    """
    domains = (marketplaces.get_marketplace(market) or {}).get("domains") or {}
    for region in domains:
        return None if region == "*" else region
    return None


def _surfaces(market: str) -> dict:
    """What the code actually provides for one marketplace, read off the thing that does the work.

    `connect` is deliberately absent: it needs nothing beyond an adapter and a registry entry, so
    it is exactly `offer`.
    """
    adapter = market_adapters.get_adapter(market)
    if adapter is None:
        return {}
    urls = marketplaces.urls(market)
    return {
        # 1 + 2 — offered at onboarding, and switchable on the /sellee card.
        "offer": market in market_adapters.connectable_markets(_a_region_it_serves(market)),
        # 3 — listing to it; asked of `supported_markets` so the guard cannot drift from the
        # reader every other caller uses.
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
        # 6 — getting back in when the session drops; `login_js` has no default, so an adapter
        # cannot be built without one.
        "signin": bool(adapter.login_js),
        # 7 — answering buyers **by any transport**, which is the promise connecting a marketplace
        # actually makes. Added because grading `inbox` alone graded the *mechanism*: craigslist
        # has no on-site inbox and never will, so that entry is permanently waived — and a
        # permanent waiver on a surface that is in fact delivered reads as a gap forever. This is
        # the promise, and craigslist keeps it through `sellee/mail/`.
        "answers_buyers": market_adapters.answers_buyers(market),
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
    """Without this a waiver written during a gap would sit forever, excusing a surface that had
    worked for months."""
    provided = _surfaces(market)
    assert provided, f"waiver names {market!r}, which has no adapter"
    assert surface in provided, f"waiver names an unknown surface {surface!r}"
    assert not provided[surface], (
        f"{market} now delivers {surface!r} — delete its waiver in {__name__}."
    )


def test_signin_cannot_be_waived_away() -> None:
    """The one surface with no legitimate gap: a market nobody can sign back in to is dead the
    first time it logs out."""
    assert not [pair for pair in WAIVERS if pair[1] == "signin"]
    for market in _markets():
        assert _surfaces(market)["signin"]
