"""The market-adapter seam: everything a marketplace does differently, in one module each.

An adapter carries the per-market facts — where the inbox is, how to pull thread links off it,
how to read a chat's tail, where the composer is, how to tell whether the seller is logged in,
and which publish recipe to load. The generic layer above (client, inbox lane, reconcile, reply
sink, publish plumbing) knows only this protocol, so a new marketplace is a new module plus a
registry entry, not edits threaded through the layer — the same split `channel/` uses for providers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from sellee import marketplaces
from sellee.browser.markets import carousell, craigslist, facebook


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
    # What the seller already has listed, read off their own listings page:
    # `{listings: [{listing_id, url, title, price, price_text}], active_count, dropped, truncated}`
    # or `{error: …}`. Live listings only — a reader that cannot prove the rows are live must
    # answer with the error, since everything downstream treats these as adoptable.
    my_listings_js: str = ""
    # One listing's own page: `{active, title, description, price, currency, condition, photo_urls}`
    # or null to abstain. Read after a yes: the fields an item needs, the photographs, and `active`
    # — what stops a late yes relisting something that has since sold.
    listing_detail_js: str = ""
    # Where the listing id sits in a permalink, as a regex with one group — what joins a
    # conversation to one of our items.
    listing_id_pattern: str = ""
    # For a market whose inbox is a folder inside a general messages app: JS that marks the control
    # opening it, and the selector that mark creates. The control ignores a click dispatched from
    # the page, so the caller clicks the mark for real. Empty when the inbox is just a page.
    inbox_folder_js: str = ""
    inbox_folder_target: str = ""
    # Which listing the open conversation is about, for a market that names it only there. Read
    # once when first seen; empty when the list already carries `product_id`.
    product_id_js: str = ""
    # JS answering `{url}` for a market whose listings page sits behind a link rather than at a
    # fixed address; the survey follows it before reading `my_listings_js`.
    my_listings_entry_js: str = ""
    # Publishing by driving the form rather than by a recipe a model reads. These move together: a
    # market has them or it has a `listing_flow`, and `supported_markets` asks for either.
    # `publish_fields_js` marks every control and says which it found; `publish_readback_js` says
    # what the form holds; `publish_options_js(wanted)` marks one dropdown option;
    # `publish_result_js` names the listing that was made.
    publish_fields_js: str = ""
    publish_readback_js: str = ""
    publish_result_js: str = ""
    publish_target: Callable[[str], str] = lambda step: ""
    publish_options_js: Callable[[str], str] = lambda wanted: ""
    # Where a driver files a listing when nothing better is known — choosing a category from a
    # title is the listing flow's judgement, not a driver's.
    publish_default_category: str = ""
    # This market's own word for an item's free-text condition — the vocabulary its condition
    # dropdown offers is the market's, so the mapping lives with the market, not the driver.
    publish_condition_for: Callable[[str], str] = lambda said: ""
    # The reply composer's shipped selector defaults, by step.
    composer: tuple = ()
    # Rows an inbox read should never treat as a buyer conversation.
    system_handles: frozenset = field(default_factory=frozenset)
    # A row preview meaning the marketplace itself says there is nothing here to read, matched
    # case-insensitively against the list row's preview. Without it, a withdrawn conversation is
    # indistinguishable from one our own reader failed on, and the two want opposite answers.
    empty_preview_pattern: str = ""
    # This market's own wording for its verification wall (`blindness.CAUSE_VERIFY`) — what the
    # wall asks for is the market's to name. Empty falls back to blindness.py's generic sentence.
    verify_notice: str = ""
    # The market's own responsive breakpoint: below this it is liable to serve a layout the
    # readers cannot parse, and a failed read may be the window's fault rather than the market's.
    # 0 means no width is too narrow. Only a floor — it never claims a read failed, it only
    # reframes one that already did.
    min_usable_width_px: int = 0

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
    min_usable_width_px=carousell.MIN_USABLE_WIDTH_PX,
)

FACEBOOK = MarketAdapter(
    market="fb",
    conversations_list_js=facebook.CONVERSATIONS_LIST_JS,
    conversation_tail_js=facebook.CONVERSATION_TAIL_JS,
    login_js=facebook.LOGIN_JS,
    my_listings_js=facebook.MY_LISTINGS_JS,
    listing_detail_js=facebook.LISTING_DETAIL_JS,
    my_listings_entry_js=facebook.MY_LISTINGS_ENTRY_JS,
    publish_fields_js=facebook.PUBLISH_FIELDS_JS,
    publish_readback_js=facebook.PUBLISH_READBACK_JS,
    publish_result_js=facebook.PUBLISH_RESULT_JS,
    publish_target=facebook.publish_target,
    publish_options_js=facebook.options_js,
    publish_default_category=facebook.DEFAULT_CATEGORY,
    publish_condition_for=facebook.condition_for,
    listing_id_pattern=facebook.LISTING_ID_PATTERN,
    inbox_folder_js=facebook.INBOX_FOLDER_JS,
    inbox_folder_target=facebook.INBOX_FOLDER_TARGET,
    product_id_js=facebook.PRODUCT_ID_JS,
    composer=tuple(Selector(**row) for row in facebook.COMPOSER_DEFAULTS),
    system_handles=facebook.SYSTEM_HANDLES,
    empty_preview_pattern=facebook.EMPTY_PREVIEW_PATTERN,
    verify_notice=facebook.VERIFY_NOTICE,
    min_usable_width_px=facebook.MIN_USABLE_WIDTH_PX,
)

CRAIGSLIST = MarketAdapter(
    market="craigslist",
    # There is no on-site inbox to read: craigslist routes buyers to an anonymised relay email.
    # These are the honest answer rather than placeholders — see `markets/craigslist.py`. The market
    # still carries an `inbox` URL, because the read lane's login probe and the survey backfill ride
    # on that same navigation, and losing them costs more than the tick does.
    conversations_list_js=craigslist.CONVERSATIONS_LIST_JS,
    conversation_tail_js=craigslist.CONVERSATION_TAIL_JS,
    login_js=craigslist.LOGIN_JS,
    my_listings_js=craigslist.MY_LISTINGS_JS,
    listing_detail_js=craigslist.LISTING_DETAIL_JS,
    listing_id_pattern=craigslist.LISTING_ID_PATTERN,
    # No `composer`, no `chat_message_submit_js`: there is nothing to type into. Publishing is the
    # `listing-flow-craigslist` recipe, so none of the `publish_*` driver fields are set either.
)

_ADAPTERS = {
    CAROUSELL.market: CAROUSELL,
    FACEBOOK.market: FACEBOOK,
    CRAIGSLIST.market: CRAIGSLIST,
}

# The flow name the composer selectors are cached under.
REPLY_FLOW = "reply"

# The composer steps an adapter may ship selectors for, under REPLY_FLOW. `send_button` is
# optional and the preferred commit where a marketplace has one: a real click needs no window
# focus and carries no `isTrusted: false`. See `sink._commit` for the precedence.
MESSAGE_BOX = "message_box"
SEND_BUTTON = "send_button"


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
        if market in _ADAPTERS and _has_a_publish_path(market)
    ]


def _has_a_publish_path(market: str) -> bool:
    """Whether anything at all knows how to put a listing on this marketplace.

    Either a recipe skill a publish pass reads or the publish selectors `browser/publisher.py`
    drives — asked of the code, never of a registry flag.
    """
    adapter = _ADAPTERS.get(market)
    return bool(marketplaces.listing_flow(market) or (adapter and adapter.publish_fields_js))


def surveyable_markets(region: str | None = None) -> list:
    """The markets whose existing listings we can read for *this seller*.

    Derived from code and registry exactly as `supported_markets` is — a marketplace becomes
    surveyable the day its adapter grows the two artifacts. Deliberately not filtered by what the
    seller has enabled: reading what they already have is how we find out whether they want anything
    managed at all.
    """
    return [market for market in marketplaces.browser_markets() if can_survey(market, region)]


def can_survey(market: str, region: str | None = None) -> bool:
    """Whether a survey of `market` could actually read anything. Called at both trigger sites, so a
    market with no adapter never gets a request row that no lane could ever serve."""
    adapter = _ADAPTERS.get(market)
    if adapter is None or not adapter.my_listings_js or not adapter.listing_detail_js:
        return False
    return marketplaces.market_url(market, "my_listings", region) is not None


def answers_buyers_in_browser(market: str) -> bool:
    """Whether this marketplace's buyers can be read and replied to *in the browser*.

    A question about the browser lane specifically: is there a conversation to open and a composer
    to type into. Craigslist has neither — its buyers reach a seller by anonymised relay email — so
    the browser lane must not go looking, and its own copy must not speak for that market.

    This is **not** the question the seller-facing copy asks. See `answers_buyers`.
    """
    adapter = _ADAPTERS.get(market)
    if adapter is None:
        return False
    return bool(adapter.composer_step(MESSAGE_BOX) and marketplaces.urls(market).get("thread"))


def reads_buyers(market: str) -> bool:
    """Whether the agent can *read* this marketplace's buyers, by any transport.

    True for a browser inbox, and true for craigslist — whose buyers arrive as mail, which
    `sellee/mail/` reads. Separate from answering them, because for craigslist those two questions
    have different answers and the difference is measured.
    """
    if answers_buyers_in_browser(market):
        return True
    from sellee.connectables import MAIL_MARKETS

    return market in MAIL_MARKETS


# Markets whose buyers can be read but **not replied to**, with the evidence. A capability flag
# rather than an architectural claim, so it flips back with one line if the evidence changes.
#
# **Craigslist: measured 2026-09-10, one posting (id 7963439877), one seller account.**
#
#   buyer -> seller, `<hex>@sale.craigslist.org`      worked 3 times out of 3
#   seller -> buyer, `<hex>@reply.craigslist.org`     accepted, no bounce, never arrived  (2x)
#   seller -> buyer, `<hex>@sale.craigslist.org`      accepted, no bounce, never arrived  (1x)
#   seller -> buyer, a **hand-typed** reply, no automation in the path, never arrived     (1x)
#   seller -> buyer, a *stale* hex                    bounced 550 "get a current reply email
#                                                     address" — so the relay is not a blind
#                                                     catch-all; it has routing state and chose to
#                                                     accept and discard the live ones
#
# The hand-typed control is what makes this a fact about craigslist rather than about this code.
# Nothing craigslist documents predicts it: their help page says contact information "passes
# through unaltered" and that threads "continue for up to 4 months", and their relay-error page
# lists no silent-drop case. Both relay domains resolve the same MX (`mxia.craigslist.org`), so it
# is not a send-only domain either.
#
# So the promise is withdrawn rather than kept badly: reading a craigslist buyer is real and
# useful — the seller hears about them in chat instead of watching an inbox — and replying is
# theirs. If a clean account is later observed replying successfully, delete the entry.
READ_ONLY_BUYERS = ("craigslist",)


def answers_buyers(market: str) -> bool:
    """Whether the agent can answer this marketplace's buyers **at all**, by any transport.

    Connecting a marketplace has always promised four things — list to it, read its inbox, answer
    its buyers, adopt what is already there. Craigslist keeps three: its relay carries a buyer's
    message to the seller and has never been observed carrying a reply back, including from a
    hand-typed send with none of this code involved (see `READ_ONLY_BUYERS`).

    A *capability* question, about the code and what has been measured of the marketplace. Whether
    a given seller's buyers can be answered right now also needs their mailbox connected, which is
    `can_answer_buyers`. Copy shown to a seller asks that one.
    """
    if market in READ_ONLY_BUYERS:
        return False
    return reads_buyers(market)


def can_answer_buyers(market: str, store) -> bool:
    """Whether *this seller's* buyers on this market can be answered right now.

    **This is the predicate every seller-facing promise branches on**, and it is a function of
    state rather than of code alone. Craigslist can answer buyers — but only once the mailbox those
    buyers email has been signed in to, scoped and confirmed. Before that, the honest thing to say
    is that their replies are theirs.

    Mechanised deliberately. The alternative is a set of strings someone has to remember to flip in
    the same commit that wires the transport, and getting that wrong is the same bug in either
    direction: promising replies that do not happen, or disclaiming replies the agent is sending.
    """
    if not answers_buyers(market):
        # No transport has ever been observed answering this market's buyers, so no amount of
        # seller state makes it true.
        return False
    if answers_buyers_in_browser(market):
        return True
    from sellee.connectables import MAIL_MARKETS

    if market not in MAIL_MARKETS:
        return False
    return bool(store.mail_ready(market))


def drivable_markets() -> list:
    """Every browser market we have an adapter for, in registry order — regardless of seller.

    A fact about the code, so a pure settings parser may ask it; where a given seller can be is
    `connectable_markets`. Deliberately weaker than `supported_markets`: a market can be readable
    long before anything can list to it, and connecting should not wait on a publish path.
    """
    return [market for market in marketplaces.browser_markets() if market in _ADAPTERS]


def connectable_markets(region: str | None = None) -> list:
    """The browser markets *this seller* can connect: one we can drive, on a site where they are.

    A marketplace with a site in the seller's own country sorts first — registry order alone would
    read as a recommendation. Sorted off the registry rather than a per-region list, so a
    marketplace that adds a regional site sorts up on its own.
    """
    connectable = [
        market
        for market in drivable_markets()
        if marketplaces.resolve_domain(market, region) is not None
    ]
    # Stable, so registry order still breaks ties within each group.
    return sorted(
        connectable, key=lambda market: not marketplaces.has_regional_site(market, region)
    )


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
