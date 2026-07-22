"""The shipped marketplace registry: region→host resolution and the pruned-stub guard."""

from __future__ import annotations

from selly_agent import marketplaces


def test_resolve_regional_host_exact() -> None:
    assert marketplaces.resolve_domain("carousell", "SG") == "www.carousell.sg"
    assert marketplaces.resolve_domain("carousell", "MY") == "www.carousell.com.my"


def test_resolve_falls_back_to_star_default() -> None:
    # fb has only a "*" domain; ebay has regional hosts plus a "*" default for unknown regions
    assert marketplaces.resolve_domain("fb", "SG") == "www.facebook.com"
    assert marketplaces.resolve_domain("ebay", "ZZ") == "www.ebay.com"
    assert marketplaces.resolve_domain("fb", None) == "www.facebook.com"


def test_resolve_falls_back_to_listing_url_host() -> None:
    # craigslist has no domains map; the listing_url host is the last-resort answer
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
