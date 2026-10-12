"""No marketplace is handed browser authority for a publish without a recipe for using it.

`_publish_browser_tools` gates on one thing — is this market's connector a browser — so every
browser marketplace in the registry gets the same twelve tools, including `browser_tabs`,
main-world `browser_evaluate` and `browser_handle_dialog`. What tells the model what to *do* with
them is a separate lookup (`marketplaces.listing_flow`), and nothing tied the two together. That is
how Facebook came to be the one market with the whole diet and no recipe at all: a model with free
navigation of a logged-in account and no instructions, which is also a model able to dismiss a
warning the seller ought to see.

Reflecting over the registry rather than naming markets, because the gap appears when a marketplace
is *added* — a hand-written test names the markets that existed when it was written, and a new
registry entry with a browser connector and no skill file would pass every one of them.
"""

from __future__ import annotations

from sellee import marketplaces, passes
from sellee.browser import publisher


def test_a_browser_grant_always_comes_with_a_recipe() -> None:
    offenders = {}
    for entry in marketplaces.all_marketplaces():
        market = entry["id"]
        if not passes._publish_browser_tools({"market": market}, None, "p1"):
            continue
        if not marketplaces.listing_flow(market):
            offenders[market] = "browser tools granted with no listing_flow recipe"
    assert offenders == {}


def test_a_driven_market_is_never_granted_browser_tools() -> None:
    # A market with a deterministic driver does not get a model pass at all, so a grant here would
    # be authority for work no model is supposed to be doing.
    offenders = {
        entry["id"]
        for entry in marketplaces.all_marketplaces()
        if publisher.can_drive(entry["id"])
        and passes._publish_browser_tools({"market": entry["id"]}, None, "p1")
    }
    assert offenders == set()
