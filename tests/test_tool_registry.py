"""Registry: schema validation, secret masking, tier filtering, and the dispatch event trail."""

from __future__ import annotations

import pytest

import selly_agent.tools  # noqa: F401  ensures every tool is registered
from selly_agent.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_PUBLISH,
    ToolError,
    UnknownTool,
    dispatch,
    tools_for_tier,
)
from selly_agent.tools.schema import ValidationError, validate

# --- the JSON-Schema subset -------------------------------------------------------------------


def test_validate_accepts_conforming_object() -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "n": {"type": "integer"}},
        "required": ["a"],
        "additionalProperties": False,
    }
    validate(schema, {"a": "x", "n": 3})


@pytest.mark.parametrize(
    "value, match",
    [
        ({"n": 3}, "missing required"),
        ({"a": 5}, "expected string"),
        ({"a": "x", "n": "notint"}, "expected integer"),
        ({"a": "x", "extra": 1}, "unknown property"),
    ],
)
def test_validate_rejects(value, match) -> None:
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "n": {"type": "integer"}},
        "required": ["a"],
        "additionalProperties": False,
    }
    with pytest.raises(ValidationError, match=match):
        validate(schema, value)


def test_validate_enum() -> None:
    schema = {"type": "object", "properties": {"s": {"type": "string", "enum": ["x", "y"]}}}
    validate(schema, {"s": "x"})
    with pytest.raises(ValidationError, match="one of"):
        validate(schema, {"s": "z"})


def test_validate_bool_is_not_a_number() -> None:
    schema = {"type": "object", "properties": {"n": {"type": "number"}}}
    with pytest.raises(ValidationError):
        validate(schema, {"n": True})


def test_validate_additional_properties_true_allows_extra() -> None:
    schema = {"type": "object", "properties": {}, "additionalProperties": True}
    validate(schema, {"anything": 1, "goes": "here"})


# --- tier filtering ---------------------------------------------------------------------------


def test_tools_for_tier_partitions_the_surface() -> None:
    attended = {s.name for s in tools_for_tier(TIER_ATTENDED)}
    publish = {s.name for s in tools_for_tier(TIER_PASS_PUBLISH)}
    assert {"create_item", "set_floor", "list_items"} <= attended
    # the publish pass tier is minimal — no floor/create/config surface
    assert publish == {
        "get_item",
        "carousell_ai_upload_photos",
        "carousell_ai_publish_listing",
        "send_message",
    }
    assert "set_floor" not in publish
    assert "create_item" not in publish


def test_dispatch_refuses_a_tier_hidden_tool_indistinguishably(make_ctx) -> None:
    ctx = make_ctx(TIER_PASS_PUBLISH)
    # set_floor exists but is not in this tier: refused exactly like a nonexistent tool.
    with pytest.raises(UnknownTool, match="unknown tool"):
        dispatch("set_floor", {"item_id": "x", "floor": 5, "source": "seller"}, ctx)
    with pytest.raises(UnknownTool, match="unknown tool"):
        dispatch("does_not_exist", {}, ctx)


# --- dispatch: validation, masking, event trail -----------------------------------------------


def _events(bus, kind):
    return [e for e in bus.store.read() if e.kind == kind]


def test_dispatch_validation_failure_raises_toolerror_and_events(make_ctx, bus) -> None:
    ctx = make_ctx(TIER_ATTENDED)
    with pytest.raises(ToolError):
        dispatch("get_item", {}, ctx)  # missing required item_id
    # a rejected call is observable, but never ran a handler (no tool.call)
    assert _events(bus, "tool.error")
    assert not _events(bus, "tool.call")


def test_dispatch_happy_path_publishes_call_then_result(make_ctx, bus, store) -> None:
    ctx = make_ctx(TIER_ATTENDED)
    item = store.create_item(title="Lamp", list_price=10.0, currency="SGD")
    result = dispatch("get_item", {"item_id": item["id"]}, ctx)
    assert result["id"] == item["id"]
    calls = _events(bus, "tool.call")
    results = _events(bus, "tool.result")
    assert [c.payload["tool"] for c in calls] == ["get_item"]
    assert [r.payload["tool"] for r in results] == ["get_item"]


def test_secret_param_is_masked_in_every_event(make_ctx, bus, store) -> None:
    ctx = make_ctx(TIER_ATTENDED)
    item = store.create_item(title="Lamp", list_price=100.0, currency="SGD")
    dispatch("set_floor", {"item_id": item["id"], "floor": 63.75, "source": "seller"}, ctx)
    # the floor value must appear in no stored event payload, anywhere.
    for event in bus.store.read():
        assert "63.75" not in str(event.payload)
    call = _events(bus, "tool.call")[0]
    assert call.payload["params"]["floor"] == "***"


def test_pass_id_rides_every_dispatch_event(make_ctx, bus, store) -> None:
    ctx = make_ctx(TIER_PASS_PUBLISH, pass_id="pass_abc")
    item = store.create_item(title="Lamp", list_price=10.0, currency="SGD")
    dispatch("get_item", {"item_id": item["id"]}, ctx)
    for event in _events(bus, "tool.call") + _events(bus, "tool.result"):
        assert event.pass_id == "pass_abc"
