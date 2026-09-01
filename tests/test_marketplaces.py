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


def test_resolve_falls_back_to_listing_url_host() -> None:
    # craigslist has no domains map and a real host, so the listing_url host is the answer
    assert marketplaces.resolve_domain("craigslist", "US") == "craigslist.org"


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
    assert market_adapters.supported_markets() == ["fb", "carousell"]


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
    """Carousell runs no US site, but Facebook serves everywhere, so a US seller still has one
    marketplace."""
    assert market_adapters.publishable_markets("SG") == ["fb", "carousell"]
    assert market_adapters.publishable_markets("US") == ["fb"]
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
