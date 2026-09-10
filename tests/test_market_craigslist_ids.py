"""One craigslist posting, two addresses, and the id space they must agree on.

Craigslist serves the same posting under an older `<city>.craigslist.org/…/<numeric>.html` and the
canonical `www.craigslist.org/view/d/<slug>/<token>` it 301-redirects to. The id in one is not the
id in the other, and nothing in either URL carries both — so which form gets *recorded* decides
whether a buyer's message and the seller's own listings page ever describe the same thing.

Both URLs below are the same real posting, captured from a live publish on 2026-09-09.
"""

from __future__ import annotations

from sellee.browser import reconcile
from sellee.browser.markets.craigslist import LISTING_ID_PATTERN

LEGACY = (
    "https://sfbay.craigslist.org/sfc/hsh/d/san-francisco-desk-lamp-grey-works-fine/7963324125.html"
)
CANONICAL = (
    "https://www.craigslist.org/view/d/san-francisco-desk-lamp-grey-works-fine/"
    "kYA5WSQXhG8jRAGTiue3Bd"
)


def _id(url: str) -> str | None:
    return reconcile.listing_id(url, LISTING_ID_PATTERN)


def test_both_url_shapes_yield_an_id() -> None:
    """Both are live and either may be encountered — a stored URL from before this rule, or a link
    read off a page — so neither may read as "not a listing"."""
    assert _id(LEGACY) == "7963324125"
    assert _id(CANONICAL) == "kYA5WSQXhG8jRAGTiue3Bd"


def test_the_two_shapes_disagree_and_that_is_the_hazard() -> None:
    """Pinned deliberately, because it is the failure the recipe is written to avoid rather than a
    property to be pleased about.

    The seller's account page links the canonical form. If a publish records the legacy form, the
    survey later reads the canonical id off that same posting, finds no item claiming it, and
    adopts the seller's own already-managed listing a second time — a duplicate item, a duplicate
    rail listing and a duplicate fan-out, all silent. Hence the recipe records the account page's
    URL, not the confirmation page's.
    """
    assert _id(LEGACY) != _id(CANONICAL)


def test_the_canonical_shape_is_what_the_account_page_serves() -> None:
    """Captured live from the populated account page: its row links `/view/d/<slug>/<token>`, so
    the canonical id is the one both sides of the join can agree on."""
    assert _id(CANONICAL) == "kYA5WSQXhG8jRAGTiue3Bd"
    assert "/view/d/" in CANONICAL
