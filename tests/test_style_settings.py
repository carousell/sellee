"""The two style settings: persona (free text, compose-time only) and firmness (the enum that
tunes the negotiation engine), plus firmness's precedence against per-item and config knobs."""

from __future__ import annotations

import pytest
from tests.conftest import seed_setting

from sellee import settings
from sellee.config import Config
from sellee.engines import negotiate as engine
from sellee.settings import PERSONA_MAX_CHARS, SettingError
from sellee.store import Store

CFG = Config()


# --- persona ------------------------------------------------------------------------------------


def test_persona_parses_and_renders(fresh_store) -> None:
    spec = settings.get_spec("persona")
    assert spec.parse("  cheeky, give lowballers a hard time  ") == (
        "cheeky, give lowballers a hard time"
    )
    assert spec.default == ""
    assert spec.render("") == "none set"
    assert spec.render("terse") == "terse"
    assert spec.requires_approval is False  # low-stakes: wording only, undoable


def test_persona_is_length_capped(fresh_store) -> None:
    spec = settings.get_spec("persona")
    assert spec.parse("x" * PERSONA_MAX_CHARS) == "x" * PERSONA_MAX_CHARS
    with pytest.raises(SettingError, match="at most"):
        spec.parse("x" * (PERSONA_MAX_CHARS + 1))


def test_persona_rejects_a_non_string(fresh_store) -> None:
    with pytest.raises(SettingError, match="must be text"):
        settings.get_spec("persona").parse({"tone": "terse"})


# --- firmness -----------------------------------------------------------------------------------


def test_firmness_parses_the_four_levels(fresh_store) -> None:
    spec = settings.get_spec("firmness")
    for level in ("soft", "balanced", "firm", "hardline"):
        assert spec.parse(level.upper() + " ") == level
    assert spec.default == "balanced"
    assert spec.requires_approval is False


def test_firmness_rejects_an_unknown_level(fresh_store) -> None:
    with pytest.raises(SettingError, match="one of"):
        settings.get_spec("firmness").parse("brutal")


# --- firmness -> knobs: the precedence matrix -----------------------------------------------------


def test_neutral_firmness_leaves_config_speaking(fresh_store) -> None:
    """balanced expresses no opinion, so a hand-tuned config still drives the knobs (its preset
    equals the defaults anyway, so nothing silently changes for an untuned config either)."""
    tuned = Config(negotiation_max_counters=5, negotiation_min_offer_ratio=0.42)
    knobs = engine.resolve_knobs(tuned, None, "balanced")
    assert knobs["max_counters"] == 5
    assert knobs["min_offer_ratio"] == 0.42
    assert engine.resolve_knobs(tuned, None, None) == knobs  # unset firmness behaves the same


def test_firmness_preset_overrides_config(fresh_store) -> None:
    tuned = Config(negotiation_max_counters=5, negotiation_lowball_cap=9)
    knobs = engine.resolve_knobs(tuned, None, "hardline")
    assert knobs["max_counters"] == 1
    assert knobs["min_offer_ratio"] == 0.8
    assert knobs["lowball_cap"] == 1


def test_per_item_override_beats_firmness(fresh_store) -> None:
    floor_record = {"auto_counter_step": 5, "auto_counter_rounds": 7}
    knobs = engine.resolve_knobs(CFG, floor_record, "hardline")
    assert knobs["max_counters"] == 7  # the item's own setting wins
    assert knobs["step"] == 5
    assert knobs["lowball_cap"] == 1  # …but the rest of the preset still applies


def test_an_unknown_firmness_falls_back_to_config(fresh_store) -> None:
    """A level the registry would have rejected can still reach the engine through a stale stored
    value; it must read as no opinion rather than as an arbitrary preset."""
    assert engine.resolve_knobs(CFG, None, "brutal") == engine.resolve_knobs(CFG, None, None)


# --- the tool path reads the setting --------------------------------------------------------------


def _offer_with_firmness(store: Store, level: str | None, make_ctx, offer: float) -> dict:
    from sellee.tools import dispatch
    from sellee.tools.registry import TIER_ATTENDED

    if level is not None:
        seed_setting(store, "firmness", level)
    item = store.create_item(title="Thing", list_price=100.0, currency="SGD")
    store.set_floor(item["id"], 40.0, "seller")
    return dispatch(
        "negotiate_offer",
        {"item_id": item["id"], "thread_id": "fb:1", "buyer": "b", "offer": offer},
        make_ctx(TIER_ATTENDED),
    )


def test_the_same_offer_reads_differently_at_each_firmness(store, make_ctx) -> None:
    """One 60-on-100 offer, three verdicts — the setting reaches the engine through the tool.
    soft's 0.5 ratio makes it a real offer worth countering; firm's 0.7 makes it a lowball to
    deflect; hardline's 0.8 plus its cap of one exhausts the patience immediately and disengages."""
    assert _offer_with_firmness(store, "soft", make_ctx, 60)["decision"] == "counter"
    assert _offer_with_firmness(store, "firm", make_ctx, 60)["decision"] == "deflect_lowball"
    assert _offer_with_firmness(store, "hardline", make_ctx, 60)["decision"] == "hold_firm"


def test_the_default_firmness_applies_with_nothing_stored(store, make_ctx) -> None:
    # balanced: 55 is below 0.6 * 100, so it is a lowball (soft would have countered it)
    assert _offer_with_firmness(store, None, make_ctx, 55)["decision"] == "deflect_lowball"
    assert _offer_with_firmness(store, "soft", make_ctx, 55)["decision"] == "counter"
