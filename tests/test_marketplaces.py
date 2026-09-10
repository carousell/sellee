"""The shipped marketplace registry: region→host resolution and the pruned-stub guard."""

from __future__ import annotations

import dataclasses

from sellee import marketplaces
from sellee.browser import markets as market_adapters
from sellee.engines import hosts


def test_resolve_regional_host_exact() -> None:
    assert marketplaces.resolve_domain("carousell", "SG") == "www.carousell.sg"
    assert marketplaces.resolve_domain("carousell", "MY") == "www.carousell.com.my"


def test_resolve_falls_back_to_star_default() -> None:
    # fb has only a "*" domain; ebay has regional hosts plus a "*" default for unknown regions
    assert marketplaces.resolve_domain("fb", "SG") == "www.facebook.com"
    assert marketplaces.resolve_domain("ebay", "ZZ") == "www.ebay.com"
    assert marketplaces.resolve_domain("fb", None) == "www.facebook.com"


def test_resolve_falls_back_to_listing_url_host(monkeypatch) -> None:
    """An entry with no domains map at all falls back to its listing host.

    Pinned against a synthetic entry rather than a real one: craigslist used to be the only
    map-less entry and now has a map, and the next entry to gain one would silently take this
    coverage with it.
    """
    monkeypatch.setattr(
        marketplaces,
        "get_marketplace",
        lambda market: {"id": "mapless", "listing_url": {"host": "mapless.example"}},
    )
    for region in ("US", "SG", None):
        assert marketplaces.resolve_domain("mapless", region) == "mapless.example"


def test_resolve_unknown_market_is_none() -> None:
    assert marketplaces.resolve_domain("nope", "SG") is None


def test_display_name_known_and_fallback() -> None:
    assert marketplaces.display_name("carousell-ai") == "Carousell.ai"
    assert marketplaces.display_name("nope") == "nope"  # fail-open to the id


def test_carousell_ai_entry_shape() -> None:
    entry = marketplaces.get_marketplace("carousell-ai")
    assert entry["listing_url"]["host"] == "www.carousell.ai"
    assert entry["connector"]["type"] == "mcp"


def test_recipe_less_stubs_are_pruned() -> None:
    ids = {e["id"] for e in marketplaces.all_marketplaces()}
    assert {"depop", "thredup", "nextdoor"}.isdisjoint(ids)
    # the kept first-port markets are present
    assert {"fb", "carousell", "carousell-ai"}.issubset(ids)


def test_registry_carries_no_unread_fields() -> None:
    """The registry is the data no code can derive: hosts, URL templates, display names. A field
    nothing reads is a fact free to drift, so it does not live here."""
    unread = {"regions", "categories", "fulfillment", "default_enabled"}
    for entry in marketplaces.all_marketplaces():
        assert unread.isdisjoint(entry), entry["id"]
        assert set(entry.get("connector") or {}) <= {"type"}, entry["id"]


def test_allowlist_covers_markets_without_adapters() -> None:
    """Why the registry cannot shrink to the markets we drive: entries with no adapter still
    contribute the hosts that keep the scam scanner from flagging legitimate marketplace links."""
    allowlist = hosts.build_allowlist(marketplaces.all_marketplaces())
    assert {"ebay.com", "mercari.com", "poshmark.com"} <= allowlist


def test_supported_markets_is_the_adapter_registry() -> None:
    """The markets something knows how to publish to — every other browser entry is a host the
    scanner needs, not a market anything can drive."""
    assert market_adapters.supported_markets() == ["fb", "carousell", "craigslist"]


def test_a_publish_path_is_a_recipe_or_a_driver(monkeypatch) -> None:
    """A recipe skill a pass reads, or the publish selectors the driver fills — both need an
    adapter."""
    monkeypatch.setattr(marketplaces, "listing_flow", lambda market: "")
    assert market_adapters.supported_markets() == ["fb"]

    monkeypatch.undo()
    monkeypatch.setattr(market_adapters, "_ADAPTERS", {})
    assert market_adapters.supported_markets() == []


def test_a_market_with_neither_recipe_nor_driver_cannot_be_published_to(monkeypatch) -> None:
    """The capability is read off the code that implements it: an adapter with neither is not
    publishable."""
    stripped = dataclasses.replace(market_adapters.FACEBOOK, publish_fields_js="")
    monkeypatch.setattr(marketplaces, "listing_flow", lambda market: "")
    monkeypatch.setattr(market_adapters, "_ADAPTERS", {"fb": stripped})

    assert market_adapters.supported_markets() == []


def test_publishable_markets_follow_the_seller_region() -> None:
    """Carousell runs no US site and craigslist no Singapore one, while Facebook serves everywhere —
    so the answer differs per seller and a region we know nothing about gets only the catch-all."""
    assert market_adapters.publishable_markets("SG") == ["fb", "carousell"]
    assert market_adapters.publishable_markets("US") == ["fb", "craigslist"]
    assert market_adapters.publishable_markets(None) == ["fb"]


# --- region resolution: a domains map is exhaustive --------------------------------------------


def test_a_region_absent_from_the_map_has_no_site() -> None:
    assert marketplaces.resolve_domain("carousell", "US") is None
    assert marketplaces.resolve_domain("carousell", None) is None


def test_carousell_ai_serves_us_and_sg_only() -> None:
    assert marketplaces.resolve_domain("carousell-ai", "US") == "www.carousell.ai"
    assert marketplaces.resolve_domain("carousell-ai", "SG") == "www.carousell.ai"
    assert marketplaces.resolve_domain("carousell-ai", "MY") is None


def test_no_entry_ever_resolves_to_a_bare_host_suffix() -> None:
    """A suffix like "carousell." is the verifier's host pattern. Handed out as a site it composes
    URLs that cannot resolve and region checks that compare against nonsense."""
    for entry in marketplaces.all_marketplaces():
        for region in ("SG", "US", "MY", "ZZ", None):
            host = marketplaces.resolve_domain(entry["id"], region)
            assert host is None or not host.endswith("."), (entry["id"], region, host)


# --- craigslist: one market, surfaces on several hosts -----------------------------------------


def test_craigslist_serves_the_us_and_nowhere_else() -> None:
    """The map is exhaustive, so a region absent from it is a region the market does not serve.
    Before it existed, the no-map fallback handed `craigslist.org` to *every* region including
    Singapore, which would have offered the market to a seller with no buyers on it."""
    assert marketplaces.resolve_domain("craigslist", "US") == "www.craigslist.org"
    assert marketplaces.resolve_domain("craigslist", "SG") is None
    assert marketplaces.resolve_domain("craigslist", None) is None


def test_craigslist_listing_urls_still_verify_under_the_mapped_host() -> None:
    """`www.craigslist.org` strips to `craigslist.org`, so both the canonical host and a legacy
    per-city one satisfy the verifier. A per-city value in the map would have failed closed on
    every other city."""
    entry = marketplaces.get_marketplace("craigslist")
    region_host = marketplaces.resolve_domain("craigslist", "US")
    for url in (
        "https://www.craigslist.org/view/d/some-thing/aBcDeF",
        "https://sfbay.craigslist.org/sfc/msa/d/some-thing/7963311351.html",
    ):
        ok, reason = hosts.verify_listing_pattern(
            url, entry["listing_url"]["host"], entry["listing_url"]["path"], region_host
        )
        assert ok, (url, reason)

    # A different marketplace's link does not pass as craigslist's.
    ok, _ = hosts.verify_listing_pattern(
        "https://www.facebook.com/marketplace/item/123",
        entry["listing_url"]["host"],
        entry["listing_url"]["path"],
        region_host,
    )
    assert not ok


def test_an_absolute_url_template_reaches_another_host_of_the_same_market() -> None:
    """Craigslist's posting flow lives on `post.craigslist.org` while its listings live on `www.`,
    and `urls` values are otherwise paths glued onto the one resolved host."""
    assert (
        marketplaces.market_url("craigslist", "sell", "US", area="sfo")
        == "https://post.craigslist.org/c/sfo"
    )


def test_an_absolute_template_still_answers_none_for_an_unserved_region() -> None:
    """The region gate is not bypassed by an absolute value: a seller with no craigslist site has
    nowhere to post, whichever host the template names."""
    assert marketplaces.market_url("craigslist", "sell", "SG", area="sfo") is None


def test_a_template_placeholder_with_no_field_is_none_not_a_literal() -> None:
    """The whole reason the format is unconditional. Left as `if fields else path`, an unset area
    handed the caller `https://post.craigslist.org/c/{area}` — a truthy, unusable URL that reads as
    success and would be navigated to."""
    assert marketplaces.market_url("craigslist", "sell", "US") is None


def test_relative_templates_are_unchanged_by_the_absolute_escape_hatch() -> None:
    assert marketplaces.market_url("fb", "inbox", "US") == "https://www.facebook.com/messages/"
    assert marketplaces.market_url("carousell", "sell", "SG") == "https://www.carousell.sg/sell"
    assert marketplaces.market_url("carousell", "my_listings", "SG") == (
        "https://www.carousell.sg/manage-listings/"
    )


def test_craigslists_account_pages_are_read_on_the_host_that_serves_them() -> None:
    """`www.craigslist.org/account` is an iframe shell — `window.cl.init(..., 'framedApplication')`
    — whose content is another origin, so a page script cannot read into it. The legacy accounts
    host is server-rendered and, signed out, 302s to a real login form, which is what makes a
    three-state login probe possible at all."""
    legacy = "https://accounts.craigslist.org/login/home"
    assert marketplaces.market_url("craigslist", "my_listings", "US") == legacy
    assert marketplaces.market_url("craigslist", "inbox", "US") == legacy


def test_a_thread_template_without_its_id_is_none() -> None:
    """A `{thread_id}` left unformatted used to compose a URL pointing at a page that cannot
    exist; None makes the caller report it instead."""
    assert marketplaces.market_url("fb", "thread", "US") is None
    assert (
        marketplaces.market_url("fb", "thread", "US", thread_id="123")
        == "https://www.facebook.com/messages/t/123/"
    )


def test_market_home_prefers_a_recorded_home_page() -> None:
    """Craigslist's front page proves nothing about the session — signed out it shows an `account`
    link and no login form — so the login probe needs a page where both states are legible."""
    assert marketplaces.market_home("craigslist", "US") == (
        "https://accounts.craigslist.org/login/home"
    )


def test_market_home_is_still_the_front_page_without_one() -> None:
    assert marketplaces.market_home("fb", "US") == "https://www.facebook.com/"
    assert marketplaces.market_home("carousell", "SG") == "https://www.carousell.sg/"
    assert marketplaces.market_home("craigslist", "SG") is None


def test_craigslist_photos_come_from_the_host_it_actually_serves_them_from() -> None:
    """Captured from a live listing rather than guessed: a wrong media host fetches no photos, and
    an adopted listing with no photos is never relisted onto the rail at all."""
    assert marketplaces.media_hosts("craigslist") == ["images.craigslist.org"]


def test_craigslist_is_offered_to_a_us_seller_and_nobody_else() -> None:
    assert "craigslist" in market_adapters.drivable_markets()
    assert "craigslist" in market_adapters.connectable_markets("US")
    assert "craigslist" not in market_adapters.connectable_markets("SG")
    assert "craigslist" not in market_adapters.connectable_markets(None)


def test_craigslist_leads_the_offer_for_a_us_seller() -> None:
    """The connect offer puts a marketplace with a site in the seller's own country first, because
    registry order alone would read as a recommendation. Craigslist names US exactly; Facebook only
    has a catch-all."""
    assert market_adapters.connectable_markets("US")[0] == "craigslist"


def test_craigslist_publishes_by_recipe_not_by_driver() -> None:
    """The posting wizard is conditional per area and every step POSTs to a one-time URL, so there
    is nothing stable for the deterministic driver to fill — the publish path is a skill a pass
    reads, and the skill file has to exist or the pass improvises from house conventions alone."""
    from sellee import skills

    assert marketplaces.listing_flow("craigslist") == "listing-flow-craigslist"
    assert "listing-flow-craigslist" in skills.available()
    assert not market_adapters.get_adapter("craigslist").publish_fields_js


def test_craigslist_answers_no_buyers_in_the_browser() -> None:
    """There is no on-site inbox, so the adapter ships no composer and the registry records no
    thread URL — which is what keeps the surfaces guard honestly reporting the gap."""
    adapter = market_adapters.get_adapter("craigslist")
    assert adapter.composer == ()
    assert adapter.chat_message_submit_js == ""
    assert "thread" not in marketplaces.urls("craigslist")
