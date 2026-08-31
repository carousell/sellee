"""The shared host-boundary matcher: strict marketplace matching, the checkout carve-out, link
extraction, and defang — all pure, no network."""

from __future__ import annotations

import pytest

from sellee import marketplaces
from sellee.engines import hosts

BASE = "https://www.carousell.ai/checkout"


@pytest.fixture
def allowlist():
    return hosts.build_allowlist(marketplaces.all_marketplaces())


def test_marketplace_match_is_boundary_exact(allowlist) -> None:
    assert hosts.host_is_marketplace("www.facebook.com", allowlist)
    assert hosts.host_is_marketplace("carousell.sg", allowlist)
    assert hosts.host_is_marketplace("www.ebay.com.sg", allowlist)
    assert hosts.host_is_marketplace("deals.carousell.sg", allowlist)  # subdomain ok
    # lookalikes fail closed
    assert not hosts.host_is_marketplace("facebook.com.scam.site", allowlist)
    assert not hosts.host_is_marketplace("carousell-pay.net", allowlist)
    assert not hosts.host_is_marketplace("faceb00k.com", allowlist)
    assert not hosts.host_is_marketplace("notcarousell.sg", allowlist)
    assert not hosts.host_is_marketplace("carousell.ai.scam.site", allowlist)
    assert not hosts.host_is_marketplace("", allowlist)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.carousell.ai/checkout/abc123", True),
        ("https://www.carousell.ai/checkout/abc123?listing_id=xyz", True),
        ("https://www.carousell.ai@evil.com/checkout/x", False),  # userinfo trick
        ("https://www.carousell.ai.scam.site/checkout/x", False),  # subdomain lookalike
        ("https://carousell.ai/checkout/x", False),  # bare apex host, not www.
        ("http://www.carousell.ai/checkout/x", False),  # not https
        ("https://xn--carousel-x.ai/checkout/x", False),  # punycode
        ("https://www.carousell.ai/pay/x", False),  # wrong path
        ("https://www.carousell.ai/checkout/", False),  # no sale id
        ("https://www.carousell.ai:8080/checkout/x", False),  # odd port
    ],
)
def test_checkout_carveout(url, expected) -> None:
    assert hosts.is_checkout_link(url, BASE) is expected


def test_checkout_base_is_config_derived() -> None:
    # keys on the configured base host exactly — nothing is hardcoded to a carousell.ai shape
    assert hosts.is_checkout_link(
        "https://shop.example.com/checkout/x", "https://shop.example.com/checkout"
    )
    assert not hosts.is_checkout_link(
        "https://www.carousell.ai/checkout/x", "https://shop.example.com/checkout"
    )


def test_extract_links_scheme_bare_and_dedup() -> None:
    links = hosts.extract_links("see https://phish.top/steal and fastpay.top and plain words")
    assert "https://phish.top/steal" in links
    assert "fastpay.top" in links
    # a bare token with no plausible TLD and no path is not a link
    assert hosts.extract_links("meet me at 3pm.ish tomorrow") == []


def test_defang_neutralises() -> None:
    d = hosts.defang("https://phish.top/steal")
    assert d.startswith("hxxps://") and "[.]" in d and "phish.top" not in d


def test_verify_listing_pattern_region_gate() -> None:
    ok, _ = hosts.verify_listing_pattern(
        "https://www.carousell.sg/p/thing-123", "carousell.", "/p/", "www.carousell.sg"
    )
    assert ok
    bad, reason = hosts.verify_listing_pattern(
        "https://www.carousell.com.my/p/thing-123", "carousell.", "/p/", "www.carousell.sg"
    )
    assert not bad and "regional" in reason
    fab, _ = hosts.verify_listing_pattern(
        "https://fb.com/item/abc", "facebook.com", "/marketplace/item/"
    )
    assert not fab
