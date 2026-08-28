"""Thread & want tools: identity-complete creation, the field/status-constrained updater,
hold/release (lossless prior status, re-hold keeps original, mark-handled advances the cursor),
and want cancel (idempotent, refuses bought, closes open threads, never touches the budget)."""

from __future__ import annotations

import pytest

from sellee.tools.registry import ToolError, dispatch


def _item(store):
    return store.create_item(title="Lamp", list_price=80.0, currency="SGD")


def _want(store):
    return store.create_want(query="iPhone 15")


# --- create_thread ----------------------------------------------------------------------------


def test_create_thread_requires_identity(make_ctx, store) -> None:
    ctx = make_ctx("attended")
    item = _item(store)
    thread = dispatch(
        "create_thread",
        {
            "thread_id": "fb:1",
            "side": "sell",
            "market": "fb",
            "counterpart_handle": "bob",
            "item_id": item["id"],
        },
        ctx,
    )
    assert thread["thread_id"] == "fb:1" and thread["status"] == "active"
    # a sell thread with no item_id is refused
    with pytest.raises(ToolError, match="item_id"):
        dispatch(
            "create_thread",
            {"thread_id": "fb:2", "side": "sell", "market": "fb", "counterpart_handle": "x"},
            ctx,
        )


def test_create_thread_verifies_listing_url(make_ctx, store) -> None:
    ctx = make_ctx("attended")
    item = _item(store)
    with pytest.raises(ToolError, match="verification"):
        dispatch(
            "create_thread",
            {
                "thread_id": "fb:3",
                "side": "sell",
                "market": "fb",
                "counterpart_handle": "bob",
                "item_id": item["id"],
                "listing_url": "https://fb.com/item/fake",  # wrong host → fails closed
            },
            ctx,
        )


# --- update_thread ------------------------------------------------------------------------------


def test_update_thread_field_and_status_constraints(make_ctx, store) -> None:
    ctx = make_ctx("attended")
    item = _item(store)
    store.create_thread(
        thread_id="fb:1", side="sell", market="fb", counterpart_handle="b", item_id=item["id"]
    )
    dispatch("update_thread", {"thread_id": "fb:1", "fields": {"agent_note": "call back"}}, ctx)
    # active -> closed is allowed here
    closed = dispatch("update_thread", {"thread_id": "fb:1", "fields": {"status": "closed"}}, ctx)
    assert closed["status"] == "closed" and closed["closed_ts"] is not None
    # a sale-state transition is refused with a pointer to its owning flow
    store.create_thread(
        thread_id="fb:2", side="sell", market="fb", counterpart_handle="c", item_id=item["id"]
    )
    with pytest.raises(ToolError, match="not allowed here"):
        dispatch("update_thread", {"thread_id": "fb:2", "fields": {"status": "sold"}}, ctx)
    # transcript/cursor fields are not writable here
    with pytest.raises(ToolError, match="non-writable"):
        dispatch(
            "update_thread", {"thread_id": "fb:2", "fields": {"cursor_last_msg_id": "m9"}}, ctx
        )


def test_reactivating_around_an_open_escalation_is_refused(make_ctx, store) -> None:
    """An escalated thread refuses a send, and on 2026-08-27 a pass answered that refusal twice by
    flipping the status back to active rather than resolving the question. The escalation, not the
    status, is the substrate — so reactivating while one is open is not a thing this tool can do."""
    ctx = make_ctx("attended")
    item = _item(store)
    store.create_thread(
        thread_id="fb:1", side="sell", market="fb", counterpart_handle="b", item_id=item["id"]
    )
    store.escalate("fb:1", open_question="is it sealed or opened?", kind="question")
    assert store.get_thread("fb:1")["status"] == "escalated"

    with pytest.raises(ToolError, match="open escalation"):
        dispatch("update_thread", {"thread_id": "fb:1", "fields": {"status": "active"}}, ctx)
    assert store.get_thread("fb:1")["status"] == "escalated"

    # resolving it first is the way back in, and it clears the restore memory with it
    store.resolve_escalation(store.list_open_escalations()[0]["id"], "sealed, never opened")
    back = dispatch("update_thread", {"thread_id": "fb:1", "fields": {"status": "active"}}, ctx)
    assert back["status"] == "active"
    assert store.get_thread("fb:1")["escalated_from_status"] is None


# --- hold / release -----------------------------------------------------------------------------


def test_hold_release_lossless_and_rehold_keeps_original(make_ctx, store) -> None:
    ctx = make_ctx("attended")
    item = _item(store)
    store.create_thread(
        thread_id="fb:1", side="sell", market="fb", counterpart_handle="b", item_id=item["id"]
    )
    held = dispatch(
        "hold_thread",
        {"thread_id": "fb:1", "reason": "scam suspected", "mark_handled_msg": "m5"},
        ctx,
    )
    assert held["status"] == "held" and held["held_from_status"] == "active"
    assert held["cursor_last_msg_id"] == "m5"  # the hold advanced the cursor, no reply sent
    # a re-hold keeps the original held_from_status
    reheld = dispatch("hold_thread", {"thread_id": "fb:1", "reason": "still bad"}, ctx)
    assert reheld["held_from_status"] == "active"
    released = dispatch("release_thread", {"thread_id": "fb:1"}, ctx)
    assert released["status"] == "active" and released["held_reason"] is None


# --- wants --------------------------------------------------------------------------------------


def test_want_reads_never_include_budget(make_ctx, store) -> None:
    ctx = make_ctx("attended")
    want = _want(store)
    store.set_budget(want["want_id"], 500.0, "buyer", target_price=400.0)
    got = dispatch("get_want", {"want_id": want["want_id"]}, ctx)
    assert "max_budget" not in got and "budget" not in got


def test_update_want_field_constrained(make_ctx, store) -> None:
    ctx = make_ctx("attended")
    want = _want(store)
    dispatch("update_want", {"want_id": want["want_id"], "fields": {"target_price": 450.0}}, ctx)
    with pytest.raises(ToolError, match="non-writable"):
        dispatch("update_want", {"want_id": want["want_id"], "fields": {"status": "bought"}}, ctx)


def test_cancel_want_closes_open_threads_refuses_bought(make_ctx, store) -> None:
    ctx = make_ctx("attended")
    want = _want(store)
    store.create_thread(
        thread_id="cl:1", side="buy", market="cl", counterpart_handle="s", want_id=want["want_id"]
    )
    res = dispatch("cancel_want", {"want_id": want["want_id"], "reason": "changed mind"}, ctx)
    assert res["status"] == "cancelled" and "cl:1" in res["threads_closed"]
    assert store.get_thread("cl:1")["status"] == "closed"
    # a bought want cannot be cancelled
    bought = _want(store)
    with store._db.transaction() as conn:
        conn.execute("UPDATE wants SET status='bought' WHERE want_id=?", (bought["want_id"],))
    with pytest.raises(ToolError, match="bought"):
        dispatch("cancel_want", {"want_id": bought["want_id"]}, ctx)


def test_cancel_want_never_touches_budget(make_ctx, store) -> None:
    ctx = make_ctx("attended")
    want = _want(store)
    store.set_budget(want["want_id"], 500.0, "buyer")
    dispatch("cancel_want", {"want_id": want["want_id"]}, ctx)
    assert store.get_budget(want["want_id"])["max_budget"] == 500.0  # budget untouched
