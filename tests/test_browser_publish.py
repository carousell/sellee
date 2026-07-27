"""The publish pass across both kinds of marketplace.

One pass type serves the rail (an API call) and a browser market (a form filled in Chrome). What
differs is derived from the payload's market: which recipe rides in the system prompt, and whether
a browser is granted at all. These tests pin that the derivation happens and that the rail publish
stays browser-free.
"""

from __future__ import annotations

import pytest

from selly_agent import marketplaces, passes
from selly_agent.config import Config
from selly_agent.harness import claude

_ENDPOINT = "http://127.0.0.1:7355/mcp"


def _spec(market=None, **kwargs):
    payload = {"item_id": "item_1"}
    if market is not None:
        payload["market"] = market
    return passes.build_spec(
        passes.publish_prompt("item_1", market or passes.DEFAULT_PUBLISH_MARKET),
        _ENDPOINT,
        "TOK",
        "sonnet",
        passes.PASS_TYPES["publish"],
        payload=payload,
        browser_tools=passes.PASS_TYPES["publish"].build_browser_tools(payload, None, "p1"),
        **kwargs,
    )


def _browser_command():
    return passes.browser_command(Config())


# --- which recipe the pass carries ---------------------------------------------------------------


def test_the_rail_publish_carries_the_rail_recipe() -> None:
    spec = _spec()
    assert "carousell.ai" in spec.prompt
    assert "Carousell's own composer" not in (spec.append_system_prompt or "")


def test_a_browser_publish_carries_the_market_recipe() -> None:
    spec = _spec("carousell", browser_command=_browser_command())
    assert "Meet-up" in spec.append_system_prompt or "meet-up" in spec.append_system_prompt
    assert "List now" in spec.append_system_prompt


def test_the_recipe_comes_from_the_registry_not_a_branch() -> None:
    """Adding a marketplace should be a registry entry and a skill file, not an edit here."""
    assert marketplaces.listing_flow("carousell") == "listing-flow-carousell"
    assert marketplaces.listing_flow("carousell-ai") == "listing-flow"


def test_the_prompt_names_the_market_it_is_publishing_to() -> None:
    assert "Carousell" in passes.publish_prompt("item_1", "carousell")
    assert "carousell.ai" in passes.publish_prompt("item_1", "carousell-ai")


# --- who gets a browser -------------------------------------------------------------------------


def test_the_rail_publish_is_handed_no_browser() -> None:
    """It talks to an API. Browser authority follows the market, not the pass type."""
    spec = _spec()
    assert spec.browser_server is None
    assert not any("playwright" in rule for rule in claude.allowed_tools(spec))
    assert set(claude.mcp_config(spec)["mcpServers"]) == {"selly"}


def test_a_browser_market_publish_gets_the_diet(store) -> None:
    spec = _spec("carousell", browser_command=_browser_command())
    assert spec.browser_server is not None
    assert spec.browser_server.tools == passes.PUBLISH_BROWSER_TOOLS
    assert set(claude.mcp_config(spec)["mcpServers"]) == {"selly", "playwright"}


def test_an_absent_market_means_the_rail() -> None:
    """Every publish enqueued before browser markets existed meant the rail, and still does."""
    assert passes.publish_market({}) == passes.DEFAULT_PUBLISH_MARKET
    assert passes.PASS_TYPES["publish"].build_browser_tools({}, None, "p1") == ()


def test_a_market_the_agent_does_not_drive_gets_no_browser() -> None:
    build = passes.PASS_TYPES["publish"].build_browser_tools
    assert build({"market": "carousell-ai"}, None, "p1") == ()  # the rail
    assert build({"market": "carousell"}, None, "p1") == passes.PUBLISH_BROWSER_TOOLS


def test_publish_stays_web_free_even_with_a_browser() -> None:
    """Comps research belongs to the conversation that set the price, not to the pass that types it
    into a form."""
    spec = _spec("carousell", browser_command=_browser_command())
    assert spec.web_tools is False
    assert set(claude.WEB_TOOLS) <= set(claude.denied_tools(spec))


def test_a_browser_pass_with_no_command_available_fails_loudly() -> None:
    """Better a ledgered spawn error than a pass that starts and cannot reach its browser."""
    with pytest.raises(passes.SpawnError, match="playwright_mcp_cmd"):
        _spec("carousell", browser_command=())


def test_the_browser_command_is_the_configured_one_when_set() -> None:
    cfg = Config(playwright_mcp_cmd=["node", "/opt/mcp/cli.js"])
    assert passes.browser_command(cfg) == ("node", "/opt/mcp/cli.js")


def test_the_default_browser_command_points_at_the_configured_chrome_port() -> None:
    argv = passes.browser_command(Config(chrome_cdp_port=9333))
    assert "http://127.0.0.1:9333" in argv


# --- the enqueue path ---------------------------------------------------------------------------


def test_the_cli_can_name_a_market() -> None:
    from selly_agent import cli

    parser = cli._build_parser()  # noqa: SLF001 — the CLI's own parser is what we mean to check
    args = parser.parse_args(
        ["pass", "run", "publish", "--item", "item_1", "--market", "carousell"]
    )
    assert args.market == "carousell"
    # absent means the rail, which is what every publish before browser markets meant
    bare = parser.parse_args(["pass", "run", "publish", "--item", "item_1"])
    assert bare.market is None


def test_the_market_survives_the_queue_to_the_pass_type(store) -> None:
    """The payload is what the pass type reads to decide its recipe and its browser grant."""
    pass_id = store.enqueue_pass("publish", {"item_id": "item_1", "market": "carousell"})
    claimed = store.claim_queued_pass()
    assert claimed.pass_id == pass_id
    assert passes.publish_market(claimed.payload) == "carousell"
    build = passes.PASS_TYPES["publish"].build_browser_tools
    assert build(claimed.payload, store, pass_id) == passes.PUBLISH_BROWSER_TOOLS
