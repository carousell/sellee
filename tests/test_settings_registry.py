"""The settings registry: quiet_hours parse/render round-trips, the read helpers (registry default
when unset), and the discoverability renderers (card lines, prompt block, describe)."""

from __future__ import annotations

import pytest

from selly_agent import settings

# --- quiet_hours parse / render ---------------------------------------------------------------


def test_parse_canonicalizes_pair() -> None:
    spec = settings.get_spec("quiet_hours")
    assert spec.parse([23, 9]) == [23, 9]
    assert spec.parse((23, 9)) == [23, 9]  # a tuple canonicalizes to a list


def test_render_window_and_disabled() -> None:
    spec = settings.get_spec("quiet_hours")
    assert spec.render([23, 9]) == "23:00–09:00"
    assert spec.render([0, 0]) == "disabled"  # start == end disables
    assert spec.render([8, 8]) == "disabled"


@pytest.mark.parametrize(
    "bad",
    [[23], [23, 25], [-1, 8], [23.5, 8], "night", [True, 8], {"start": 23}, [1, 2, 3]],
)
def test_parse_rejects_out_of_range_or_malformed(bad) -> None:
    spec = settings.get_spec("quiet_hours")
    with pytest.raises(settings.SettingError):
        spec.parse(bad)


def test_default_is_night_window() -> None:
    assert settings.get_spec("quiet_hours").default == [23, 8]


# --- read helpers -----------------------------------------------------------------------------


def test_get_returns_default_when_unset(fresh_store) -> None:
    assert settings.get(fresh_store, "quiet_hours") == [23, 8]


def test_get_returns_stored_value(fresh_store) -> None:
    fresh_store.apply_setting_now(
        "quiet_hours", [22, 9], change_id="chg_x", prior_value=[23, 8], notice_text="ok"
    )
    assert settings.get(fresh_store, "quiet_hours") == [22, 9]


def test_effective_covers_every_registered_key(fresh_store) -> None:
    eff = settings.effective(fresh_store)
    assert set(eff) == {spec.key for spec in settings.all_specs()}
    assert eff["quiet_hours"] == [23, 8]


def test_effective_ignores_orphan_stored_key(fresh_store, caplog) -> None:
    from tests.conftest import seed_setting

    seed_setting(fresh_store, "gone_setting", [1, 2])
    eff = settings.effective(fresh_store)  # never crashes on a stale row
    assert "gone_setting" not in eff


# --- discoverability renderers ----------------------------------------------------------------


def test_card_lists_headline_at_default(fresh_store) -> None:
    lines = settings.card_lines(fresh_store)
    assert lines == ["• Quiet hours: 23:00–08:00"]  # headline shown even at its default


def test_card_shows_changed_value(fresh_store) -> None:
    fresh_store.apply_setting_now(
        "quiet_hours", [22, 9], change_id="chg_y", prior_value=[23, 8], notice_text="ok"
    )
    assert settings.card_lines(fresh_store) == ["• Quiet hours: 22:00–09:00"]


def test_describe_carries_policy(fresh_store) -> None:
    rows = {r["key"]: r for r in settings.describe(fresh_store)}
    q = rows["quiet_hours"]
    assert q["value"] == [23, 8]
    assert q["default"] == [23, 8]
    assert q["requires_approval"] is True
    assert q["rendered"] == "23:00–08:00"


def test_prompt_block_states_propose_only(fresh_store) -> None:
    block = settings.prompt_block(fresh_store)
    assert "propose only" in block
    assert "quiet_hours" in block
