"""The settings registry: quiet_hours parse/render round-trips (HHMM integers, minute-granular),
the HHMM→minutes boundary, the read helpers (registry default when unset), and the discoverability
renderers (card lines, prompt block, describe)."""

from __future__ import annotations

import pytest
from tests.conftest import seed_setting

from sellee import marketplaces, settings
from sellee.browser import markets as market_adapters

# --- quiet_hours parse / render ---------------------------------------------------------------


def test_parse_canonicalizes_to_hhmm() -> None:
    spec = settings.get_spec("quiet_hours")
    assert spec.parse([23, 9]) == [2300, 900]  # whole hours → HHMM
    assert spec.parse((2300, 930)) == [2300, 930]  # HHMM integers, minute-granular
    assert spec.parse(["23:00", "09:30"]) == [2300, 930]  # HH:MM strings also accepted


def test_render_window_and_disabled() -> None:
    spec = settings.get_spec("quiet_hours")
    assert spec.render([2300, 930]) == "23:00–09:30"
    assert spec.render([2230, 715]) == "22:30–07:15"
    assert spec.render([0, 0]) == "disabled"  # start == end disables
    assert spec.render([900, 900]) == "disabled"


@pytest.mark.parametrize(
    "bad",
    [
        [23],  # not a pair
        [1, 2, 3],  # not a pair
        [2360, 900],  # 60 minutes is invalid
        [2500, 900],  # past 2400
        [25, 9],  # 25 is neither a valid hour nor a valid HHMM
        [-1, 8],
        [23.5, 8],  # not an int
        "night",
        [True, 8],  # bool is not an hour
        {"start": 23},
    ],
)
def test_parse_rejects_out_of_range_or_malformed(bad) -> None:
    spec = settings.get_spec("quiet_hours")
    with pytest.raises(settings.SettingError):
        spec.parse(bad)


def test_default_is_night_window() -> None:
    assert settings.get_spec("quiet_hours").default == [2300, 800]


# --- connected_markets -------------------------------------------------------------------------


def test_crosslist_parse_canonicalizes() -> None:
    spec = settings.get_spec("connected_markets")
    assert spec.parse(["carousell", "carousell"]) == ["carousell"]  # de-duplicated
    assert spec.parse(" Carousell ") == ["carousell"]  # a bare name, case-folded
    assert spec.parse([]) == []
    assert spec.parse("") == []


def test_crosslist_render() -> None:
    spec = settings.get_spec("connected_markets")
    assert spec.render([]) == "none — carousell.ai only"
    assert spec.render(["carousell"]) == "Carousell"


@pytest.mark.parametrize(
    "bad",
    [
        ["carousell-ai"],  # the rail is where everything goes; it is not a member of this list
        ["ebay"],  # an allowlist-only registry entry
        ["nope"],
        {"markets": ["carousell"]},
        5,
    ],
)
def test_crosslist_refuses_markets_it_cannot_publish_to(bad) -> None:
    spec = settings.get_spec("connected_markets")
    with pytest.raises(settings.SettingError) as excinfo:
        spec.parse(bad)
    assert "Carousell" in str(excinfo.value)  # the refusal names what is supported


def test_crosslist_default_is_rail_only() -> None:
    spec = settings.get_spec("connected_markets")
    assert spec.default == []
    assert spec.requires_approval is True  # posting publicly as the seller goes through the door


def test_crosslist_helper_drops_a_no_longer_publishable_value(fresh_store, monkeypatch) -> None:
    """A stored market that stops being publishable — an adapter withdrawn, or a region that does
    not serve it — must not leave an eligible publish behind."""
    from tests.conftest import seed_setting

    seed_setting(fresh_store, "connected_markets", ["carousell"])
    fresh_store.set_seller_config_section("basics", {"region": "SG"})
    assert settings.publish_markets(fresh_store) == ["carousell"]

    fresh_store.set_seller_config_section("basics", {"region": "US"})
    assert settings.publish_markets(fresh_store) == []

    fresh_store.set_seller_config_section("basics", {"region": "SG"})
    monkeypatch.setattr(market_adapters, "_ADAPTERS", {})
    assert settings.publish_markets(fresh_store) == []


def test_check_for_seller_says_so_when_nothing_at_all_is_available(
    fresh_store, monkeypatch
) -> None:
    """With no drivable market, the missing region is the thing the seller can act on, so that is
    what the refusal says."""
    monkeypatch.setattr(market_adapters, "_ADAPTERS", {})
    with pytest.raises(settings.SettingError) as excinfo:
        settings.check_for_seller("connected_markets", ["carousell"], fresh_store)
    assert "which country you sell in" in str(excinfo.value)


def test_a_market_needing_a_region_is_refused_but_offers_one_that_does_not(fresh_store) -> None:
    """A missing region is not a blanket refusal: Facebook serves everywhere, so the seller is
    pointed at what they *can* have."""
    with pytest.raises(settings.SettingError) as excinfo:
        settings.check_for_seller("connected_markets", ["carousell"], fresh_store)

    message = str(excinfo.value)
    assert "Carousell isn't available" in message
    assert "Facebook Marketplace" in message


def test_check_for_seller_refuses_a_market_with_no_site_in_the_region(fresh_store) -> None:
    fresh_store.set_seller_config_section("basics", {"region": "US"})
    with pytest.raises(settings.SettingError) as excinfo:
        settings.check_for_seller("connected_markets", ["carousell"], fresh_store)
    assert "US accounts" in str(excinfo.value)


def test_check_for_seller_passes_a_served_market(fresh_store) -> None:
    fresh_store.set_seller_config_section("basics", {"region": "SG"})
    settings.check_for_seller("connected_markets", ["carousell"], fresh_store)
    settings.check_for_seller(
        "connected_markets", [], fresh_store
    )  # clearing it never region-fails


def test_check_for_seller_normalizes_a_lowercase_region(fresh_store) -> None:
    """The registry keys its sites by code, so a seller recorded as "sg" must still resolve."""
    fresh_store.set_seller_config_section("basics", {"region": " sg "})
    assert fresh_store.seller_region() == "SG"
    settings.check_for_seller("connected_markets", ["carousell"], fresh_store)


def test_check_for_seller_ignores_settings_with_no_seller_dependency(fresh_store) -> None:
    settings.check_for_seller("quiet_hours", [2300, 800], fresh_store)


# --- raise_browser -----------------------------------------------------------------------------


def test_raise_browser_parse_accepts_booleans_and_words() -> None:
    spec = settings.get_spec("raise_browser")
    assert spec.parse(True) is True
    assert spec.parse(False) is False
    assert spec.parse("true") is True
    assert spec.parse(" False ") is False
    assert spec.parse("on") is True
    assert spec.parse("no") is False


@pytest.mark.parametrize("bad", ["sometimes", 1, 0, [], {}, None, "yes please"])
def test_raise_browser_parse_rejects_non_booleans(bad) -> None:
    spec = settings.get_spec("raise_browser")
    with pytest.raises(settings.SettingError) as excinfo:
        spec.parse(bad)
    assert "true or false" in str(excinfo.value)  # the refusal says what would be accepted


def test_raise_browser_render() -> None:
    spec = settings.get_spec("raise_browser")
    assert spec.render(True) == "comes to the front when I open a page for you"
    assert spec.render(False) == "stays in the background"


def test_raise_browser_defaults_on_and_applies_immediately() -> None:
    spec = settings.get_spec("raise_browser")
    assert spec.default is True
    assert spec.requires_approval is False  # the seller's own UX preference — no approval door


def test_raise_browser_stays_off_the_default_card() -> None:
    """The raise itself and the setup question are the hints this knob exists, so it earns no
    headline slot — it appears on the card only once changed from its default."""
    assert "raise_browser" not in settings.CARD_HEADLINE


# --- watch_browser -----------------------------------------------------------------------------


def test_watch_browser_parse_accepts_booleans_and_words() -> None:
    spec = settings.get_spec("watch_browser")
    assert spec.parse(True) is True
    assert spec.parse("on") is True
    assert spec.parse(" Off ") is False
    assert spec.parse("no") is False


@pytest.mark.parametrize("bad", ["sometimes", 1, 0, [], {}, None, "watch me"])
def test_watch_browser_parse_rejects_non_booleans(bad) -> None:
    spec = settings.get_spec("watch_browser")
    with pytest.raises(settings.SettingError) as excinfo:
        spec.parse(bad)
    assert "true or false" in str(excinfo.value)  # the refusal says what would be accepted


def test_watch_browser_render_says_which_way_round() -> None:
    spec = settings.get_spec("watch_browser")
    assert "you'll see" in spec.render(True)
    assert "background" in spec.render(False)


def test_watch_browser_defaults_off_and_applies_immediately() -> None:
    spec = settings.get_spec("watch_browser")
    assert spec.default is False
    assert spec.requires_approval is False  # the seller's own UX preference — no approval door


def test_watch_browser_is_on_the_default_card() -> None:
    """Unlike raise_browser, nothing the agent ever says would hint that where it works is a
    choice — and the card carries the button that flips it, which needs its state legible."""
    assert "watch_browser" in settings.CARD_HEADLINE


def test_watch_browser_description_warns_the_raise_is_a_mac_thing() -> None:
    # This text is the model's whole vocabulary for the setting (prompt_block), so a promise it
    # cannot keep on Linux would be made in the seller's chat.
    spec = settings.get_spec("watch_browser")
    assert "Mac" in spec.description


# --- read helpers + the minutes boundary ------------------------------------------------------


def test_get_returns_default_when_unset(fresh_store) -> None:
    assert settings.get(fresh_store, "quiet_hours") == [2300, 800]


def test_get_returns_stored_value(fresh_store) -> None:
    fresh_store.apply_setting_now(
        "quiet_hours", [2230, 715], change_id="chg_x", prior_value=[2300, 800], notice_text="ok"
    )
    assert settings.get(fresh_store, "quiet_hours") == [2230, 715]


def test_quiet_window_minutes_converts(fresh_store) -> None:
    assert settings.quiet_window_minutes(fresh_store) == (1380, 480)  # 23:00 / 08:00 default
    fresh_store.apply_setting_now(
        "quiet_hours", [2230, 715], change_id="chg_m", prior_value=[2300, 800], notice_text="ok"
    )
    assert settings.quiet_window_minutes(fresh_store) == (1350, 435)  # 22:30 / 07:15


def test_effective_covers_every_registered_key(fresh_store) -> None:
    eff = settings.effective(fresh_store)
    assert set(eff) == {spec.key for spec in settings.all_specs()}
    assert eff["quiet_hours"] == [2300, 800]


def test_effective_ignores_orphan_stored_key(fresh_store) -> None:
    from tests.conftest import seed_setting

    seed_setting(fresh_store, "gone_setting", [1, 2])
    assert "gone_setting" not in settings.effective(fresh_store)  # never crashes on a stale row


# --- discoverability renderers ----------------------------------------------------------------


def test_card_lists_headline_at_default(fresh_store) -> None:
    # connected_markets is absent by design: the card renders it as its own Connections block,
    # so a value line here would say it twice.
    assert settings.card_lines(fresh_store) == [
        "• Quiet hours: 23:00–08:00",
        "• Watch mode: off — I work in the background",
        "7 more settings at defaults — ask me about settings.",
    ]


def test_a_setting_with_its_own_card_section_is_not_also_a_value_line(fresh_store) -> None:
    """Even once changed from its default — the rule is about where it is rendered."""
    seed_setting(fresh_store, "connected_markets", ["carousell"])

    assert not any("marketplace" in line.lower() for line in settings.card_lines(fresh_store))


def test_card_shows_changed_value(fresh_store) -> None:
    fresh_store.apply_setting_now(
        "quiet_hours", [2230, 715], change_id="chg_y", prior_value=[2300, 800], notice_text="ok"
    )
    assert settings.card_lines(fresh_store)[0] == "• Quiet hours: 22:30–07:15"


def test_card_promotes_a_changed_non_headline_setting(fresh_store) -> None:
    """A non-headline setting is invisible at its default and appears once changed — the card
    scales with what the seller has customized, not with the catalog."""
    assert not any("Persona" in line for line in settings.card_lines(fresh_store))
    fresh_store.apply_setting_now(
        "persona", "terse and businesslike", change_id="chg_p", prior_value="", notice_text="ok"
    )
    lines = settings.card_lines(fresh_store)
    assert "• Persona: terse and businesslike" in lines
    assert "6 more settings at defaults — ask me about settings." in lines


def test_describe_carries_policy(fresh_store) -> None:
    q = {r["key"]: r for r in settings.describe(fresh_store)}["quiet_hours"]
    assert q["value"] == [2300, 800]
    assert q["default"] == [2300, 800]
    assert q["requires_approval"] is True
    assert q["rendered"] == "23:00–08:00"


def test_prompt_block_states_propose_only(fresh_store) -> None:
    block = settings.prompt_block(fresh_store)
    assert "propose only" in block
    assert "quiet_hours" in block


# --- craigslist area: the rollout gate, and the country check ---------------------------------


def test_craigslist_area_defaults_to_the_launch_area() -> None:
    """With one launched area there is nothing for a seller to choose, so the default is the
    answer and the setup step never appears."""
    assert settings.get_spec("craigslist_area").default == "sfo"


def test_craigslist_area_stores_the_abbreviation_whatever_the_seller_named() -> None:
    """The posting flow requires the abbreviation; the hostname is what a seller can see and copy.
    Feeding the hostname to the flow answers HTTP 200 and lands on a generic area picker, so the
    two must never be interchangeable past this parser."""
    parse = settings.get_spec("craigslist_area").parse
    for said in ("sfo", "sfbay", "SF bay area", "https://www.craigslist.org/area/sfbay"):
        assert parse(said) == "sfo"


def test_craigslist_area_refuses_an_area_we_have_not_launched() -> None:
    """Los Angeles is inside the legal envelope and still not somewhere we work — the refusal has
    to read as a rollout limit, not a broken account."""
    with pytest.raises(settings.SettingError) as caught:
        settings.get_spec("craigslist_area").parse("lax")
    assert "yet" in str(caught.value).lower()


def test_craigslist_area_refuses_an_area_outside_the_legal_envelope() -> None:
    with pytest.raises(settings.SettingError) as caught:
        settings.get_spec("craigslist_area").parse("nyc")
    assert "yet" not in str(caught.value).lower()


def test_craigslist_area_refuses_an_area_that_does_not_exist() -> None:
    with pytest.raises(settings.SettingError):
        settings.get_spec("craigslist_area").parse("atlantis")


def test_craigslist_area_renders_as_a_place() -> None:
    assert settings.get_spec("craigslist_area").render("sfo") == "SF bay area (sfbay)"


def test_craigslist_area_refuses_an_area_in_another_country(fresh_store) -> None:
    """Craigslist runs a US site and the seller sells in Singapore: posting their goods to the Bay
    Area reaches nobody who can buy them. Checked against seller state rather than in the parser,
    which is pure and knows nothing about this seller."""
    fresh_store.set_seller_config_section("basics", {"region": "SG"})
    with pytest.raises(settings.SettingError) as caught:
        settings.check_for_seller("craigslist_area", "sfo", fresh_store)
    assert "SG" in str(caught.value)


def test_craigslist_area_passes_for_a_seller_in_its_country(fresh_store) -> None:
    fresh_store.set_seller_config_section("basics", {"region": "US"})
    settings.check_for_seller("craigslist_area", "sfo", fresh_store)


def test_craigslist_area_is_not_refused_for_an_unknown_region(fresh_store) -> None:
    """Deliberately permissive, matching `connected_markets`: a seller whose region we have not
    asked for yet is not told their area is wrong. The country is only compared when known."""
    settings.check_for_seller("craigslist_area", "sfo", fresh_store)


def test_clearing_the_area_is_never_region_refused(fresh_store) -> None:
    fresh_store.set_seller_config_section("basics", {"region": "SG"})
    settings.check_for_seller("craigslist_area", "", fresh_store)


# --- the posting URL's per-seller field --------------------------------------------------------


def test_market_url_fields_supplies_the_area_for_craigslist(fresh_store) -> None:
    """`urls.sell` for craigslist carries `{area}`, and `market_url` formats unconditionally — so
    without this the caller would get None and the publish would refuse."""
    assert settings.market_url_fields("craigslist", fresh_store) == {"area": "sfo"}


def test_market_url_fields_is_empty_for_every_other_market(fresh_store) -> None:
    """No other market's templates take a field at this call site, and handing one an unexpected
    key would be a silent behaviour change."""
    for market in ("fb", "carousell", "carousell-ai", "nope"):
        assert settings.market_url_fields(market, fresh_store) == {}


def test_market_url_fields_omits_an_unset_area(fresh_store) -> None:
    """Fail closed: with no area, `market_url` answers None and the publish refuses up front,
    rather than composing `.../c/{area}` and navigating to it."""
    seed_setting(fresh_store, "craigslist_area", "")
    assert settings.market_url_fields("craigslist", fresh_store) == {}
    assert (
        marketplaces.market_url(
            "craigslist", "sell", "US", **settings.market_url_fields("craigslist", fresh_store)
        )
        is None
    )


# --- the postal code craigslist's map step will not proceed without ----------------------------


def test_craigslist_postal_code_defaults_to_unset() -> None:
    assert settings.get_spec("craigslist_postal_code").default == ""


@pytest.mark.parametrize("said", ["94103", " 94103 ", "94103-1234"])
def test_a_us_postal_code_is_accepted(said) -> None:
    assert settings.get_spec("craigslist_postal_code").parse(said).startswith("94103")


@pytest.mark.parametrize("bad", ["abc", "9410", "0", "not a zip", "941035551212"])
def test_a_value_that_is_not_a_postal_code_is_refused(bad) -> None:
    """Never guessed and never coerced: this decides where on a map the seller's item appears, and
    a wrong one puts it in the wrong place rather than failing loudly."""
    with pytest.raises(settings.SettingError):
        settings.get_spec("craigslist_postal_code").parse(bad)


def test_clearing_the_postal_code_is_allowed() -> None:
    assert settings.get_spec("craigslist_postal_code").parse("") == ""
