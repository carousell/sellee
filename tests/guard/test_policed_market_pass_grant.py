"""A marketplace that polices automation is never handed to a model with a browser.

Every page load the daemon makes of such a market goes through the page-load governor, through
`BrowserClient.navigate`. A pass drives a Playwright server of its own, which the governor never
sees, so a browser grant to a pass on one of these markets would be a way around every limit it
sets. Checked over the adapters rather than a list of names, so a market added later is covered.
"""

from __future__ import annotations

import pytest

from sellee import passes
from sellee.browser import markets as market_adapters

POLICED = [adapter.market for adapter in market_adapters.adapters() if adapter.polices_automation]


def test_there_is_a_policed_market_to_check() -> None:
    assert "fb" in POLICED


@pytest.mark.parametrize("market", POLICED)
def test_no_publish_pass_on_a_policed_market_gets_a_browser(market) -> None:
    assert passes._publish_browser_tools({"market": market}, None, "p1") == ()


@pytest.mark.parametrize("market", POLICED)
@pytest.mark.parametrize(
    "changed", [("title",), ("photos",), ("condition",), ("title", "photos", "list_price")]
)
def test_no_edit_pass_on_a_policed_market_gets_a_browser(market, changed) -> None:
    """Including the changes the editor cannot drive itself, which are the ones that would
    otherwise be a pass's to make."""
    assert passes._edit_browser_tools({"market": market, "changed": changed}, None, "p1") == ()
