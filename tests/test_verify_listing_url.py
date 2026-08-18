"""verify_listing_url tool: pure host/path/region checks for browser markets, and the carousell-ai
live check delegated to the rail client."""

from __future__ import annotations

from sellee.rail.client import RailToolError
from sellee.tools.registry import dispatch


def test_real_permalink_passes(make_ctx) -> None:
    ctx = make_ctx("attended")
    r = dispatch(
        "verify_listing_url",
        {"market": "fb", "url": "https://www.facebook.com/marketplace/item/12345"},
        ctx,
    )
    assert r["ok"] is True


def test_fabricated_and_wrong_host_fail(make_ctx) -> None:
    ctx = make_ctx("attended")
    assert (
        dispatch("verify_listing_url", {"market": "fb", "url": "https://fb.com/item/abc"}, ctx)[
            "ok"
        ]
        is False
    )
    assert dispatch("verify_listing_url", {"market": "fb", "url": ""}, ctx)["ok"] is False


def test_region_gate(make_ctx) -> None:
    ctx = make_ctx("attended")
    ok = dispatch(
        "verify_listing_url",
        {"market": "carousell", "url": "https://www.carousell.sg/p/thing-1", "region": "SG"},
        ctx,
    )
    assert ok["ok"] is True
    wrong = dispatch(
        "verify_listing_url",
        {"market": "carousell", "url": "https://www.carousell.com.my/p/thing-1", "region": "SG"},
        ctx,
    )
    assert wrong["ok"] is False


def test_unknown_market_fails(make_ctx) -> None:
    ctx = make_ctx("attended")
    assert (
        dispatch("verify_listing_url", {"market": "nope", "url": "https://x.com/y"}, ctx)["ok"]
        is False
    )


class _FakeRail:
    def __init__(self, ok: bool):
        self._ok = ok

    def verify_listing_url(self, url: str) -> None:
        if not self._ok:
            raise RailToolError("listing page returned HTTP 404")


def test_carousell_ai_live_check_delegates_to_rail(make_ctx) -> None:
    good = make_ctx("attended", rail_factory=lambda: _FakeRail(True))
    r = dispatch(
        "verify_listing_url",
        {"market": "carousell-ai", "url": "https://www.carousell.ai/listing/1"},
        good,
    )
    assert r["ok"] is True

    bad = make_ctx("attended", rail_factory=lambda: _FakeRail(False))
    r2 = dispatch(
        "verify_listing_url",
        {"market": "carousell-ai", "url": "https://www.carousell.ai/listing/missing"},
        bad,
    )
    assert r2["ok"] is False and "404" in r2["reason"]
