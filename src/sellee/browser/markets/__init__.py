"""The market-adapter seam: everything a marketplace does differently, in one module each.

An adapter carries the per-market facts — where the inbox is, how to pull thread links off it,
how to read a chat's tail, where the composer is, how to tell whether the seller is logged in,
and which publish recipe to load. The generic layer above (client, inbox lane, reconcile, reply
sink, publish plumbing) knows only this protocol, so a new marketplace is a new module plus a
registry entry, not edits threaded through the layer — the same split `channel/` uses for providers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sellee import marketplaces
from sellee.browser.markets import carousell


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
    # JS artifacts, each a function expression for browser_evaluate. `conversations_list_js` answers
    # `{conversations: [...]}` or `{error: …}`; `conversation_tail_js` answers the trailing bubbles
    # of the open conversation, or null to abstain.
    conversations_list_js: str
    conversation_tail_js: str
    login_js: str
    # How the composed buyer-chat message is submitted, as a function taking the composer element.
    # Answers `{sent: bool, …}` — false means the page did not accept it and nothing was delivered,
    # so the caller may retry.
    #
    # Empty is the safe default: submitting falls back to a real key event, indistinguishable from a
    # person, which costs the seller their foreground. Supplying this trades that cost for a
    # keystroke dispatched from the page, carrying `isTrusted: false` — a signal on the seller's own
    # account, so it belongs to a market someone has decided that for.
    chat_message_submit_js: str = ""
    # What the seller already has listed on this marketplace, read off their own listings page:
    # `{listings: [{listing_id, url, title, price, price_text}], active_count, dropped, truncated}`
    # or `{error: …}`. Live listings only — a reader that cannot prove the rows it found are live
    # must answer with the error, because everything downstream treats these as adoptable.
    my_listings_js: str = ""
    # One listing's own page: `{active, title, description, price, currency, condition, photo_urls}`
    # or null to abstain. Read when the seller has said yes, so it does three jobs at once — the
    # fields an item needs, the photographs to bring across, and `active`, which is what stops a yes
    # tapped against an aged list from relisting something that has since sold.
    listing_detail_js: str = ""
    # Where the listing id sits in a permalink, as a regex with one group — what joins a
    # conversation to one of our items.
    listing_id_pattern: str = ""
    # The reply composer's shipped selector defaults, by step.
    composer: tuple = ()
    # Rows an inbox read should never treat as a buyer conversation.
    system_handles: frozenset = field(default_factory=frozenset)

    def composer_step(self, step: str) -> Selector | None:
        for selector in self.composer:
            if selector.step == step:
                return selector
        return None


CAROUSELL = MarketAdapter(
    market="carousell",
    conversations_list_js=carousell.CONVERSATIONS_LIST_JS,
    conversation_tail_js=carousell.CONVERSATION_TAIL_JS,
    login_js=carousell.LOGIN_JS,
    chat_message_submit_js=carousell.CHAT_MESSAGE_SUBMIT_JS,
    my_listings_js=carousell.MY_LISTINGS_JS,
    listing_detail_js=carousell.LISTING_DETAIL_JS,
    listing_id_pattern=carousell.LISTING_ID_PATTERN,
    composer=tuple(Selector(**row) for row in carousell.COMPOSER_DEFAULTS),
    system_handles=carousell.SYSTEM_HANDLES,
)

_ADAPTERS = {CAROUSELL.market: CAROUSELL}

# The flow name the composer selectors are cached under.
REPLY_FLOW = "reply"


def get_adapter(market: str) -> MarketAdapter | None:
    return _ADAPTERS.get(market)


def adapters() -> list:
    return list(_ADAPTERS.values())


def supported_markets() -> list:
    """The browser markets the agent knows how to publish to at all, in registry order: active
    entries with a registered adapter and a recorded publish recipe.

    Capability is derived from the code that implements it, never from a registry flag that could
    say yes while the adapter says no.
    """
    return [
        market
        for market in marketplaces.browser_markets()
        if market in _ADAPTERS and marketplaces.listing_flow(market)
    ]


def surveyable_markets(region: str | None = None) -> list:
    """The markets whose existing listings we can read for *this seller*: an adapter carrying both
    survey artifacts, and a recorded listings page on the regional site they are actually on.

    Derived from the code that implements it and the registry that locates it, exactly as
    `supported_markets` and `publishable_markets` are — there is no separate switch to remember, so
    a marketplace becomes surveyable the day its adapter grows the two artifacts.

    Deliberately not filtered by what the seller has enabled: reading what they already have is how
    we find out whether they want anything managed at all, and the ask itself is what turns it on.
    """
    return [market for market in marketplaces.browser_markets() if can_survey(market, region)]


def can_survey(market: str, region: str | None = None) -> bool:
    """Whether a survey of `market` could actually read anything. Called at both trigger sites, so a
    market with no adapter never gets a request row that no lane could ever serve."""
    adapter = _ADAPTERS.get(market)
    if adapter is None or not adapter.my_listings_js or not adapter.listing_detail_js:
        return False
    return marketplaces.market_url(market, "my_listings", region) is not None


def publishable_markets(region: str | None = None) -> list:
    """The browser markets *this seller* can be listed on: one we can drive, that has a site where
    they are. A marketplace with no regional site for them has nowhere to put the listing, so it is
    not a market they can enable — with no region recorded, that is true of all of them.
    """
    return [
        market
        for market in supported_markets()
        if marketplaces.resolve_domain(market, region) is not None
    ]
