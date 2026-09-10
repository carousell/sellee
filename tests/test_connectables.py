"""What a seller can sign in to — and the two things that must not blur together.

Craigslist is the first market needing two sign-ins: the site, and the mailbox its buyers reach the
seller through. A mailbox is therefore a connect *target* but not a marketplace, and the tests that
matter here are the ones that keep those apart. A mailbox that leaked into `connected_markets` would
offer the seller a marketplace to list on with no listings, and attribute Craigslist conversations
to Gmail.
"""

from __future__ import annotations

from sellee import connectables

# --- the two kinds stay apart -------------------------------------------------------------------


def test_a_market_resolves_to_its_own_site_and_probe() -> None:
    found = connectables.resolve("craigslist", "US")
    assert found is not None
    assert found.kind == connectables.KIND_MARKET
    assert found.market == "craigslist"
    assert found.display_name == "Craigslist"
    assert "craigslist.org" in (found.url or "")
    assert found.provider == ""


def test_a_mail_target_resolves_to_the_mailbox_and_names_its_market() -> None:
    """`market` is the marketplace, not the mailbox — it is what the thread's market must be, and
    what every per-market notice is keyed on."""
    found = connectables.resolve("craigslist-mail", "US")
    assert found is not None
    assert found.kind == connectables.KIND_MAIL
    assert found.is_mail is True
    assert found.market == "craigslist"
    assert "mail.google.com" in (found.url or "")
    assert found.provider == "gmail"


def test_a_market_with_its_own_inbox_has_no_mail_target() -> None:
    """Asking a Facebook seller for a mailbox is asking for a credential nothing would ever use."""
    assert connectables.mail_target_for("fb") is None
    assert connectables.mail_target_for("carousell") is None
    assert connectables.resolve("fb-mail") is None


def test_the_market_a_target_belongs_to_is_always_answerable() -> None:
    assert connectables.market_of("craigslist-mail") == "craigslist"
    assert connectables.market_of("craigslist") == "craigslist"
    assert connectables.market_of("fb") == "fb"


def test_a_mail_target_is_recognisable_without_resolving_it() -> None:
    assert connectables.is_mail_target("craigslist-mail") is True
    assert connectables.is_mail_target("craigslist") is False
    assert connectables.is_mail_target("") is False


# --- unknown ids fail the way the lane already handles ------------------------------------------


def test_an_unknown_target_resolves_to_none() -> None:
    """`None` is what `get_adapter` already returns for an unknown market, so the connect lane's
    existing "I don't know how to sign in to that" branch covers a stale mail token too."""
    for said in ("nonsense", "nonsense-mail", "", None, "  "):
        assert connectables.resolve(said) is None


# --- what this seller can connect ---------------------------------------------------------------


def test_a_mail_target_sits_directly_after_its_market() -> None:
    """Adjacent so the two-part shape of a Craigslist connection is visible wherever the list is
    rendered, rather than being two unrelated entries the seller must know belong together."""
    targets = connectables.targets_for("US")
    assert "craigslist" in targets
    assert targets[targets.index("craigslist") + 1] == "craigslist-mail"


def test_a_seller_outside_craigslists_region_is_offered_neither_half() -> None:
    """Craigslist has a US site only, so an SG seller gets no Craigslist and — the point — no
    mailbox step for a market they cannot connect."""
    targets = connectables.targets_for("SG")
    assert "craigslist" not in targets
    assert "craigslist-mail" not in targets


def test_a_market_with_no_site_for_this_seller_resolves_with_no_url() -> None:
    """`url is None` rather than a raise, so the callers that already own the wording for that case
    ("… has no site for SG") keep owning it."""
    found = connectables.resolve("craigslist", None)
    assert found is not None
    assert found.url is None


def test_the_mail_target_does_not_depend_on_region() -> None:
    """A mailbox is not a regional site. A seller whose market has no site for them still has a
    mailbox, and resolving it must not depend on the region being set."""
    assert connectables.resolve("craigslist-mail", None) is not None
    assert connectables.resolve("craigslist-mail", "SG") is not None


def test_mail_markets_has_one_answer() -> None:
    """`mail/transport.py` reads this rather than defining its own list, so "which markets are mail
    markets" cannot drift into two answers."""
    from sellee.mail import relay

    assert connectables.MAIL_MARKETS == (relay.MARKET,)
