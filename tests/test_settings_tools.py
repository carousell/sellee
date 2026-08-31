"""The LLM-facing settings tools: propose_setting_change (held / applied / unchanged / unknown /
parse error) and get_settings, plus the events they publish and their tier membership."""

from __future__ import annotations

import pytest

from sellee import settings
from sellee.channel import fastpaths
from sellee.tools import TIER_ATTENDED, TIER_PASS_CHANNEL, tools_for_tier
from sellee.tools.registry import ToolError, dispatch


def _parse_greeting(raw):
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    raise settings.SettingError("greeting must be non-empty text")


@pytest.fixture
def allow_setting():
    """A low-stakes fixture setting (no approval) so the ALLOW/immediate-apply path is exercised —
    v1's only real setting, quiet_hours, is approval-gated."""
    spec = settings.SettingSpec(
        key="test_greeting",
        label="Greeting",
        parse=_parse_greeting,
        render=lambda v: v,
        default="hi",
        description="a test greeting",
        take_effect="takes effect immediately.",
        requires_approval=False,
    )
    settings.register(spec)
    yield spec
    settings.unregister("test_greeting")


def _kinds(bus):
    return [e.kind for e in bus.store.read()]


# --- propose: HOLD (quiet_hours requires approval) --------------------------------------------


def test_propose_high_stakes_is_held(make_ctx, store, bus) -> None:
    ctx = make_ctx(TIER_PASS_CHANNEL, pass_id="p1")
    out = dispatch("propose_setting_change", {"key": "quiet_hours", "raw_value": [23, 9]}, ctx)
    assert out["status"] == "held" and out["rendered"] == "23:00–09:00"
    assert store.get_setting("quiet_hours") == [0, 0]  # unchanged — only proposed
    assert [p["change_id"] for p in store.list_pending_changes()] == [out["change_id"]]
    assert "setting.proposed" in _kinds(bus)


def test_propose_accepts_json_encoded_string_raw_value(make_ctx, store) -> None:
    # MCP clients (Claude) commonly send an untyped structured arg as a JSON-encoded string; the
    # tool decodes "[23, 9]" to the real list before the registry parser sees it.
    ctx = make_ctx(TIER_PASS_CHANNEL, pass_id="p1")
    out = dispatch("propose_setting_change", {"key": "quiet_hours", "raw_value": "[23, 9]"}, ctx)
    assert out["status"] == "held" and out["rendered"] == "23:00–09:00"


# --- propose: ALLOW (fixture low-stakes setting) ----------------------------------------------


def test_propose_low_stakes_applies_immediately(make_ctx, store, bus, allow_setting) -> None:
    ctx = make_ctx(TIER_ATTENDED)
    out = dispatch("propose_setting_change", {"key": "test_greeting", "raw_value": "  yo "}, ctx)
    assert out["status"] == "applied" and out["value"] == "yo"  # canonicalized (trimmed)
    assert settings.get(store, "test_greeting") == "yo"
    kinds = _kinds(bus)
    assert "setting.proposed" in kinds and "setting.changed" in kinds


# --- short-circuits & errors ------------------------------------------------------------------


def test_proposing_current_value_is_unchanged(make_ctx, store) -> None:
    ctx = make_ctx(TIER_ATTENDED)  # store seeds quiet_hours off = [0, 0]
    out = dispatch("propose_setting_change", {"key": "quiet_hours", "raw_value": [0, 0]}, ctx)
    assert out["status"] == "unchanged"
    assert store.list_pending_changes() == []  # no proposal row


def test_unknown_key_is_a_tool_error(make_ctx) -> None:
    ctx = make_ctx(TIER_ATTENDED)
    with pytest.raises(ToolError, match="unknown setting"):
        dispatch("propose_setting_change", {"key": "nope", "raw_value": 1}, ctx)


def test_parse_error_round_trips_as_tool_error(make_ctx) -> None:
    ctx = make_ctx(TIER_ATTENDED)
    with pytest.raises(ToolError, match="HHMM"):
        dispatch("propose_setting_change", {"key": "quiet_hours", "raw_value": [23, 99]}, ctx)


# --- get_settings -----------------------------------------------------------------------------


def test_get_settings_lists_registry(make_ctx) -> None:
    ctx = make_ctx(TIER_PASS_CHANNEL, pass_id="p1")
    out = dispatch("get_settings", {}, ctx)
    keys = {s["key"] for s in out["settings"]}
    assert "quiet_hours" in keys
    q = next(s for s in out["settings"] if s["key"] == "quiet_hours")
    assert q["requires_approval"] is True


# --- the seller-state check runs at the propose door -------------------------------------------


def test_propose_crosslist_without_a_region_is_refused(make_ctx) -> None:
    """check_for_seller is wired into the propose tool, not just callable: a value the parser
    accepts can still be refused by who the seller is."""
    ctx = make_ctx(TIER_ATTENDED)
    with pytest.raises(ToolError, match="which country"):
        dispatch(
            "propose_setting_change", {"key": "crosslist_markets", "raw_value": ["carousell"]}, ctx
        )


def test_propose_crosslist_for_an_unserved_region_is_refused(make_ctx, store) -> None:
    store.set_seller_config_section("basics", {"region": "US"})
    ctx = make_ctx(TIER_ATTENDED)
    with pytest.raises(ToolError, match="US accounts"):
        dispatch(
            "propose_setting_change", {"key": "crosslist_markets", "raw_value": ["carousell"]}, ctx
        )


def test_crosslist_applies_through_the_door(make_ctx, store, bus) -> None:
    """The whole path a real enable takes: proposed in conversation, held, approved on the phone —
    and only then read back by the fan-out's own helper."""
    store.set_seller_config_section("basics", {"region": "SG"})
    ctx = make_ctx(TIER_PASS_CHANNEL, pass_id="p1")
    out = dispatch(
        "propose_setting_change", {"key": "crosslist_markets", "raw_value": ["carousell"]}, ctx
    )
    assert out["status"] == "held" and out["rendered"] == "Carousell"
    assert settings.get(store, "crosslist_markets") == []  # nothing applied yet

    reply, _controls = fastpaths.handle_settings_door(
        store,
        bus,
        {"kind": "action", "payload": {"choice": settings.CB_APPROVE, "ref": out["change_id"]}},
    )
    assert "Applied" in reply
    assert settings.publish_markets(store) == ["carousell"]


# --- tier membership --------------------------------------------------------------------------


def test_tools_on_attended_and_channel_only() -> None:
    for tier in (TIER_ATTENDED, TIER_PASS_CHANNEL):
        names = {s.name for s in tools_for_tier(tier)}
        assert {"propose_setting_change", "get_settings"} <= names
