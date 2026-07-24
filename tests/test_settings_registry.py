"""The settings registry: quiet_hours parse/render round-trips (HHMM integers, minute-granular),
the HHMM→minutes boundary, the read helpers (registry default when unset), and the discoverability
renderers (card lines, prompt block, describe)."""

from __future__ import annotations

import pytest

from selly_agent import settings

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
    assert settings.card_lines(fresh_store) == ["• Quiet hours: 23:00–08:00"]


def test_card_shows_changed_value(fresh_store) -> None:
    fresh_store.apply_setting_now(
        "quiet_hours", [2230, 715], change_id="chg_y", prior_value=[2300, 800], notice_text="ok"
    )
    assert settings.card_lines(fresh_store) == ["• Quiet hours: 22:30–07:15"]


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
