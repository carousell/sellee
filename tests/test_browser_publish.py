"""The publish pass across both kinds of marketplace.

One pass type serves the rail (an API call) and a browser market (a form filled in Chrome). What
differs is derived from the payload's market: which recipe rides in the system prompt, and whether
a browser is granted at all. These tests pin that the derivation happens and that the rail publish
stays browser-free.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sellee import marketplaces, passes, paths, skills
from sellee.config import Config
from sellee.harness import claude
from sellee.tools.registry import ToolError, dispatch

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
    # Pinned so the specs these tests assert on stay a fixed string; unpinned the port is whatever
    # the live Chrome announced.
    return passes.browser_command(Config(chrome_cdp_port=9222))


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
    assert set(claude.mcp_config(spec)["mcpServers"]) == {"sellee"}


def test_a_browser_market_publish_gets_the_diet(store) -> None:
    spec = _spec("carousell", browser_command=_browser_command())
    assert spec.browser_server is not None
    assert spec.browser_server.tools == passes.PUBLISH_BROWSER_TOOLS
    assert set(claude.mcp_config(spec)["mcpServers"]) == {"sellee", "playwright"}


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


def test_an_unpinned_browser_command_resolves_the_port_at_spawn_time(xdg_tmp) -> None:
    """The pass is built when it is spawned, which may be long after the port was last known — and
    an unpinned Chrome that restarted in between came back on a different one. Reading the port
    Chrome announced, here, is what keeps a pass off a dead endpoint."""
    from sellee import paths
    from sellee.browser import chrome

    paths.ensure_data_dirs()
    (paths.browser_profile_dir() / chrome.ACTIVE_PORT_FILE).write_text("45123\n/devtools/browser/a")
    assert "http://127.0.0.1:45123" in passes.browser_command(Config())


# --- the enqueue path ---------------------------------------------------------------------------


def test_the_cli_can_name_a_market() -> None:
    from sellee import cli

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


# --- the photos the browser is allowed to see ----------------------------------------------------
#
# The browser's file upload reads only the directories the Playwright server treats as its workspace
# roots, and the media store is not one of them. So a browser publish is handed copies, and the
# prompt and the recipe both have to point at those rather than at what get_item reports.


def _item_with_photos(store, count=2, suffix=".jpg"):
    media = paths.media_dir()
    media.mkdir(parents=True, exist_ok=True)
    photos = []
    for index in range(1, count + 1):
        src = media / f"shot-{index}{suffix}"
        src.write_bytes(b"\xff\xd8\xff\xd9")  # smallest thing that is recognisably a file
        photos.append({"path": str(src)})
    return store.create_item(title="Lamp", list_price=40.0, currency="SGD", photos=photos)


def test_a_browser_publish_stages_its_photos_into_the_workspace(store, xdg_tmp, tmp_path) -> None:
    item = _item_with_photos(store)
    workspace = tmp_path / "ws"
    staged = passes._stage_photos(  # noqa: SLF001 — the staging is the unit under test
        workspace, {"item_id": item["id"], "market": "carousell"}, store
    )
    assert staged == ("01.jpg", "02.jpg")
    assert sorted(p.name for p in workspace.iterdir()) == ["01.jpg", "02.jpg"]


def test_a_rail_publish_stages_nothing(store, xdg_tmp, tmp_path) -> None:
    """Its upload runs in the daemon and reads the media store directly — a copy would be waste."""
    item = _item_with_photos(store)
    workspace = tmp_path / "ws"
    assert passes._stage_photos(workspace, {"item_id": item["id"]}, store) == ()  # noqa: SLF001
    assert not workspace.exists()


def test_an_item_with_no_photos_stages_nothing(store, xdg_tmp, tmp_path) -> None:
    item = store.create_item(title="Lamp", list_price=40.0, currency="SGD")
    workspace = tmp_path / "ws"
    payload = {"item_id": item["id"], "market": "carousell"}
    assert passes._stage_photos(workspace, payload, store) == ()  # noqa: SLF001


def test_a_photo_that_has_gone_missing_is_skipped_not_fatal(store, xdg_tmp, tmp_path) -> None:
    """The recipe verifies what it uploaded and reports a shortfall; refusing to publish at all
    because one file went astray is the worse failure."""
    item = _item_with_photos(store, count=2)
    Path(item["photos"][0]["path"]).unlink()
    workspace = tmp_path / "ws"
    staged = passes._stage_photos(  # noqa: SLF001
        workspace, {"item_id": item["id"], "market": "carousell"}, store
    )
    assert staged == ("02.jpg",)


def test_the_prompt_names_the_staged_files_for_a_browser_market(store, xdg_tmp) -> None:
    item = _item_with_photos(store)
    names = passes.staged_photo_names(item["id"], "carousell", store)
    assert names == ("01.jpg", "02.jpg")
    prompt = passes.publish_prompt(item["id"], "carousell", photos=names)
    assert "01.jpg, 02.jpg" in prompt
    assert "working directory" in prompt


def test_the_rail_prompt_says_nothing_about_a_working_directory(store, xdg_tmp) -> None:
    item = _item_with_photos(store)
    assert passes.staged_photo_names(item["id"], passes.DEFAULT_PUBLISH_MARKET, store) == ()
    assert "working directory" not in passes.publish_prompt(item["id"])


def test_the_recipe_does_not_send_the_pass_to_a_media_store_path() -> None:
    """The recipe used to send the pass to `get_item`'s paths, which the browser cannot read."""
    recipe = skills.load("listing-flow-carousell")
    assert "working directory" in recipe
    assert "already on disk at the paths it gives you" not in recipe


def test_the_prompt_carries_the_composer_url_from_the_registry(store, xdg_tmp) -> None:
    """The recipe forbids typing a marketplace URL from memory, so the one it needs has to arrive in
    the prompt. A live publish stalled on exactly this: the recipe said "the verified URLs you were
    given" and nothing gave any."""
    store.set_seller_config_section("basics", {"region": "SG"})
    item = _item_with_photos(store, count=1)
    prompt = passes._publish_prompt(  # noqa: SLF001
        {"item_id": item["id"], "market": "carousell"}, store, "pass_1"
    )
    assert "https://www.carousell.sg/sell" in prompt


def test_a_market_that_cannot_be_published_to_fails_the_payload(store, xdg_tmp) -> None:
    """A pass told to publish somewhere it cannot never gets spawned, so a typo is an error rather
    than eighty turns of a model discovering it has no browser and no recipe."""
    store.set_seller_config_section("basics", {"region": "SG"})
    item = _item_with_photos(store, count=1)
    for market in ("carousel", "mercari", "ebay"):
        with pytest.raises(passes.PassPayloadError):
            passes.validate_payload("publish", {"item_id": item["id"], "market": market}, store)


def test_a_market_with_no_site_in_the_sellers_region_fails_the_payload(store, xdg_tmp) -> None:
    store.set_seller_config_section("basics", {"region": "US"})
    item = _item_with_photos(store, count=1)
    with pytest.raises(passes.PassPayloadError):
        passes.validate_payload("publish", {"item_id": item["id"], "market": "carousell"}, store)
    # The rail is region-independent here and always allowed through.
    passes.validate_payload("publish", {"item_id": item["id"], "market": "carousell-ai"}, store)


def test_a_publish_payload_without_an_item_fails(store, xdg_tmp) -> None:
    passes.validate_payload("channel", {}, store)  # only publish has payload preconditions
    with pytest.raises(passes.PassPayloadError):
        passes.validate_payload("publish", {"market": "carousell"}, store)


def test_the_composer_url_is_the_sellers_own_region(store, xdg_tmp) -> None:
    store.set_seller_config_section("basics", {"region": "MY"})
    item = _item_with_photos(store, count=1)
    prompt = passes._publish_prompt(  # noqa: SLF001
        {"item_id": item["id"], "market": "carousell"}, store, "pass_1"
    )
    assert "https://www.carousell.com.my/sell" in prompt


def test_a_market_with_no_recorded_composer_gets_no_url_rather_than_a_guess(store, xdg_tmp) -> None:
    """market_url answers None for an unrecorded template, and the prompt stays silent — the recipe
    then stops and reports instead of assembling something plausible."""
    assert marketplaces.market_url("carousell-ai", "sell", "SG") is None
    assert "composer is at" not in passes.publish_prompt("item_1")


def test_a_publish_gets_a_longer_leash_than_a_reply() -> None:
    """The cap is a runaway backstop, sized to the flow rather than shared across them."""
    assert passes.PASS_TYPES["publish"].max_turns == passes.PUBLISH_MAX_TURNS
    assert passes.PASS_TYPES["reply"].max_turns == passes.PASS_MAX_TURNS
    assert passes.PUBLISH_MAX_TURNS > passes.PASS_MAX_TURNS


def test_the_cap_reaches_the_harness_argv() -> None:
    spec = _spec(market="carousell", browser_command=_browser_command())
    assert spec.max_turns == passes.PUBLISH_MAX_TURNS
    argv = claude.pass_argv(spec)
    assert argv[argv.index("--max-turns") + 1] == str(passes.PUBLISH_MAX_TURNS)


def test_the_recipe_batches_the_field_read_back() -> None:
    """One read for three fields, not three reads."""
    recipe = skills.load("listing-flow-carousell")
    assert "ONE `browser_evaluate`" in recipe


# --- recording where the listing went live -------------------------------------------------------


def test_recording_a_published_url_joins_the_listing_to_the_item(store, make_ctx, xdg_tmp):
    """The rail's publish tool records its own result; a browser publish is filled by the pass, so
    this is the only way it comes back — and a buyer's conversation is joined to an item by exactly
    this URL, so an unrecorded listing is one whose buyers are never answered."""
    store.set_seller_config_section("basics", {"region": "SG"})
    item = _item_with_photos(store, count=1)
    ctx = make_ctx("pass:publish")
    url = "https://www.carousell.sg/p/stanley-tape-measure-3-5m-1452470530/"
    params = {"item_id": item["id"], "market": "carousell", "url": url}
    out = dispatch("record_published_listing_url", params, ctx)
    assert out["url"] == url
    assert store.get_item(item["id"])["listing_urls"]["carousell"] == url


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("https://evilcarousell.com/p/x-1/", "a lookalike domain"),
        ("https://www.carousell.sg/inbox/123/", "a page that is not a listing"),
        ("https://www.carousell.com.my/p/x-1/", "the wrong region for this seller"),
    ],
)
def test_a_url_that_is_not_this_sellers_listing_is_refused(store, make_ctx, xdg_tmp, url, why):
    """A wrong URL is worse than none: it silently attaches the wrong item, or nothing, to everyone
    who writes in. The region comes from the seller's own record, never from the caller."""
    store.set_seller_config_section("basics", {"region": "SG"})
    item = _item_with_photos(store, count=1)
    ctx = make_ctx("pass:publish")
    with pytest.raises(ToolError):
        dispatch(
            "record_published_listing_url",
            {"item_id": item["id"], "market": "carousell", "url": url},
            ctx,
        )
    assert store.get_item(item["id"])["listing_urls"] == {}, why
