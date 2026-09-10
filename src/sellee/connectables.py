"""What a seller can be signed in to — marketplaces, and the mailboxes some marketplaces need.

Craigslist is the first market that needs **two sign-ins**: the site, and the mailbox its buyers
reach the seller through. Every sign-in door in the product resolved its candidates from
`market_adapters.connectable_markets()`, so a mailbox was invisible to all of them — the installer's
marketplace phase, `/connect`, the `/sellee` card, the CLI, and the connect lane.

**A mailbox is not a marketplace, and must not become one.** `connected_markets` is the seller's
marketplace opt-in, and `tools/reply.py` refuses a send unless the thread's market is in it — a
Gmail "market" would attribute Craigslist conversations to Gmail, and offer the seller a marketplace
to list on that has no listings.

So this generalises the *target* of a sign-in instead. Everything downstream of the doors already
keys off one string; this is the resolver that turns such a string into the three things a sign-in
needs — a name to say, a URL to open, and a probe to read back. Markets keep their existing ids, so
every stored row, callback token and CLI verb that already names one keeps working; a mailbox gets
the market's id with a suffix, which makes the pair legible in a log line and in a button token.

Deliberately pure and dependency-light: it imports the registry and the two artifact modules, and
**not** `settings`. `settings.decide` needs to request a sign-in when a seller approves a
`connected_markets` change, and importing this from there must not close a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

from sellee import marketplaces
from sellee.browser import markets as market_adapters
from sellee.mail import gmail, relay

# The two kinds of thing a seller signs in to.
KIND_MARKET = "market"
KIND_MAIL = "mail"

# A mail target is its market's id plus this. Chosen over an opaque id so `craigslist-mail` reads as
# what it is wherever it turns up — a log line, a button token, `sellee connect craigslist-mail`.
MAIL_TARGET_SUFFIX = "-mail"

# The markets whose buyers arrive as mail, and therefore need the second sign-in. One entry today.
# `mail/transport.py` reads this rather than defining its own list, so "which markets are mail
# markets" has one answer.
MAIL_MARKETS = (relay.MARKET,)


@dataclass(frozen=True)
class ConnectTarget:
    """One thing a seller can sign in to.

    `url` is `None` when this seller cannot reach it — a marketplace with no site in their region.
    Kept as part of the target rather than raised here so the callers that already have the wording
    for that case ("… has no site for SG") keep owning it.
    """

    target: str
    kind: str
    # The marketplace this belongs to, for both kinds: a mail target's market is the market whose
    # buyers it carries, which is what the thread's `market` must be.
    market: str
    display_name: str
    url: str | None
    login_js: str
    # Which provider a mail target expects. Empty for a market. The probe reports what it actually
    # found, and the connect refuses a mismatch by name.
    provider: str = ""

    @property
    def is_mail(self) -> bool:
        return self.kind == KIND_MAIL


def is_mail_target(target: object) -> bool:
    """Whether this id names a mailbox rather than a marketplace."""
    return str(target or "").endswith(MAIL_TARGET_SUFFIX)


def market_of(target: object) -> str:
    """The marketplace an id belongs to, whichever kind it is.

    So a caller holding a target can always answer "which market is this about" — which is what
    `connected_markets`, the thread's market, and every per-market notice are keyed on.
    """
    said = str(target or "")
    if is_mail_target(said):
        return said[: -len(MAIL_TARGET_SUFFIX)]
    return said


def mail_target_for(market: object) -> str | None:
    """The mailbox id a market needs, or `None` for a market that has its own inbox.

    `None` is the common case and the reason this is a function rather than a suffix appended at
    call sites: Carousell and Facebook have conversations to open, so asking their sellers to
    connect a mailbox would be asking for a credential nothing would ever use.
    """
    said = str(market or "")
    if said not in MAIL_MARKETS:
        return None
    return f"{said}{MAIL_TARGET_SUFFIX}"


def _mail_target(market: str) -> ConnectTarget:
    return ConnectTarget(
        target=f"{market}{MAIL_TARGET_SUFFIX}",
        kind=KIND_MAIL,
        market=market,
        # Phrased so it reads in the existing possessive templates ("{name}'s sign-in page is
        # open…") and so the seller knows *which* mailbox is meant when they have several.
        display_name=f"your {marketplaces.display_name(market)} mailbox",
        url=gmail.SIGN_IN_URL,
        login_js=gmail.LOGIN_JS,
        provider=gmail.PROVIDER,
    )


def resolve(target: object, region: str | None = None) -> ConnectTarget | None:
    """The target for an id, or `None` if nothing can sign in to it.

    `None` mirrors what `market_adapters.get_adapter` already returns for an unknown market, so the
    lane's existing "I don't know how to sign in to that" branch covers a stale mail token too —
    the case a withdrawn registry entry or an old button produces.
    """
    said = str(target or "").strip()
    if not said:
        return None

    if is_mail_target(said):
        market = market_of(said)
        if market not in MAIL_MARKETS:
            return None
        return _mail_target(market)

    adapter = market_adapters.get_adapter(said)
    if adapter is None:
        return None
    return ConnectTarget(
        target=said,
        kind=KIND_MARKET,
        market=said,
        display_name=marketplaces.display_name(said),
        url=marketplaces.market_home(said, region),
        login_js=adapter.login_js,
    )


def targets_for(region: str | None = None) -> list:
    """Every id this seller can connect, each mail target directly after its market.

    Adjacent on purpose: wherever this list is rendered — the installer's picker, `/connect`, the
    card — the two-part shape of a Craigslist connection is visible rather than being two unrelated
    entries the seller has to know belong together.
    """
    out = []
    for market in market_adapters.connectable_markets(region):
        out.append(market)
        mail = mail_target_for(market)
        if mail:
            out.append(mail)
    return out
