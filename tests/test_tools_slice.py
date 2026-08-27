"""The slice tools end to end through dispatch: reads, writes, floor, and the send_message stub."""

from __future__ import annotations

import pytest

import sellee.tools  # noqa: F401  registration
from sellee import secrets
from sellee.config import Config
from sellee.tools.registry import TIER_ATTENDED, ToolError, dispatch


def _mk_item(ctx, **kw):
    params = {"title": "Lamp", "list_price": 80.0, "currency": "SGD"}
    params.update(kw)
    return dispatch("create_item", params, ctx)


def test_get_config_returns_non_secret_knobs(make_ctx) -> None:
    ctx = make_ctx(TIER_ATTENDED, config=Config(http_port=9000))
    cfg = dispatch("get_config", {}, ctx)
    assert cfg["http_port"] == 9000
    assert "carousell_ai_api_base" in cfg


def test_get_status_reports_provision_and_queue(make_ctx, store) -> None:
    ctx = make_ctx(TIER_ATTENDED)
    status = dispatch("get_status", {}, ctx)
    assert status["carousell_ai_provisioned"] is False
    assert status["queue_depth"] == 0
    assert status["uptime_sec"] is not None

    store.enqueue_pass("publish", {})
    secrets.write_carousell_ai_api_key("key-1")
    status2 = dispatch("get_status", {}, ctx)
    assert status2["queue_depth"] == 1
    assert status2["carousell_ai_provisioned"] is True


def test_create_get_list_items(make_ctx) -> None:
    ctx = make_ctx(TIER_ATTENDED)
    item = _mk_item(ctx)
    assert item["status"] == "draft"
    fetched = dispatch("get_item", {"item_id": item["id"]}, ctx)
    assert fetched["title"] == "Lamp"
    listing = dispatch("list_items", {}, ctx)
    assert item["id"] in [i["id"] for i in listing["items"]]


def test_get_missing_item_errors(make_ctx) -> None:
    ctx = make_ctx(TIER_ATTENDED)
    with pytest.raises(ToolError, match="no item"):
        dispatch("get_item", {"item_id": "item_nope"}, ctx)


def test_update_item_writes_and_enforces_j7(make_ctx) -> None:
    ctx = make_ctx(TIER_ATTENDED)
    item = _mk_item(ctx)
    updated = dispatch("update_item", {"item_id": item["id"], "fields": {"title": "New"}}, ctx)
    assert updated["title"] == "New"
    with pytest.raises(ToolError, match="listing_urls"):
        dispatch(
            "update_item",
            {"item_id": item["id"], "fields": {"listing_urls": {"x": "y"}}},
            ctx,
        )
    with pytest.raises(ToolError, match="unknown"):
        dispatch("update_item", {"item_id": item["id"], "fields": {"published_at": 1}}, ctx)


def test_set_floor_ack_has_no_value(make_ctx) -> None:
    ctx = make_ctx(TIER_ATTENDED)
    item = _mk_item(ctx)
    ack = dispatch("set_floor", {"item_id": item["id"], "floor": 60.0, "source": "seller"}, ctx)
    assert ack == {"status": "written", "item_id": item["id"], "source": "seller", "replaced": None}
    with pytest.raises(ToolError, match="list price"):
        dispatch("set_floor", {"item_id": item["id"], "floor": 999.0, "source": "seller"}, ctx)


def test_send_message_queues_notice_and_event(make_ctx, bus, store) -> None:
    ctx = make_ctx(TIER_ATTENDED)
    ack = dispatch("send_message", {"text": "hi", "ref": "thread-1"}, ctx)
    assert ack["queued"] is True
    # a durable notice row was written (delivery is the drain/catchup's job, not this tool's)
    queued = store.list_queued_notices()
    assert len(queued) == 1 and queued[0]["id"] == ack["notice_id"] and queued[0]["text"] == "hi"
    outs = [e for e in bus.store.read() if e.kind == "message.out"]
    assert outs and outs[0].payload["text"] == "hi" and outs[0].payload["ref"] == "thread-1"


def test_send_message_options_become_tappable_buttons(make_ctx, store) -> None:
    """What puts buttons on the listing confirmation, which is a send_message rather than an
    escalation."""
    ack = dispatch(
        "send_message",
        {"text": "Fan — $180, like new. List it?", "options": ["✅ List it", "✏️ Change something"]},
        make_ctx(TIER_ATTENDED),
    )

    queued = store.list_queued_notices()[0]
    assert queued["controls"] == [
        ["✅ List it", f"n{ack['notice_id']}:a0"],
        ["✏️ Change something", f"n{ack['notice_id']}:a1"],
    ]


def test_send_message_without_options_carries_no_buttons(make_ctx, store) -> None:
    dispatch("send_message", {"text": "your listing is live"}, make_ctx(TIER_ATTENDED))
    assert store.list_queued_notices()[0]["controls"] is None


def test_send_message_rejects_malformed_options(make_ctx, store) -> None:
    with pytest.raises(ToolError):
        dispatch(
            "send_message",
            {"text": "pick", "options": ["only one"]},
            make_ctx(TIER_ATTENDED),
        )
    assert store.list_queued_notices() == []  # rejected whole — no half-sent ask
