"""Store accessors the browser layer adds: the Q&A bank, the selector cache and its staleness
predicate, the daemon's own inbound writer, and the reply lane's unhandled-inbound query +
coalescing enqueue."""

from __future__ import annotations

import pytest

from selly_agent.store import ItemNotFound, StoreError, ThreadNotFound, ui_cache_is_stale

_DAY = 86400.0


# --- Q&A bank ---------------------------------------------------------------------------------


def test_qa_search_returns_item_and_global_rows(store) -> None:
    item = store.create_item(title="Lamp", list_price=80.0)
    other = store.create_item(title="Chair", list_price=20.0)
    store.qa_add(item["id"], "Any chips?", "One on the base.", "seller")
    store.qa_add("*", "Do you ship?", "Yes, everywhere.", "seller")
    store.qa_add(other["id"], "Colour?", "Teak.", "seller")

    questions = {row["question"] for row in store.qa_search(item["id"])}
    assert questions == {"Any chips?", "Do you ship?"}


def test_qa_search_filters_on_the_query_and_caps(store) -> None:
    item = store.create_item(title="Lamp", list_price=80.0)
    store.qa_add(item["id"], "Any chips?", "One on the base.", "seller")
    store.qa_add(item["id"], "Bulb included?", "Yes.", "seller")
    assert [r["question"] for r in store.qa_search(item["id"], "chip")] == ["Any chips?"]
    # a match on the answer counts too
    assert [r["question"] for r in store.qa_search(item["id"], "base")] == ["Any chips?"]
    assert store.qa_search(item["id"], "%") == []  # a literal wildcard matches nothing
    assert len(store.qa_search(item["id"], limit=1)) == 1


def test_qa_add_rejects_a_non_seller_source_and_a_missing_item(store) -> None:
    item = store.create_item(title="Lamp", list_price=80.0)
    with pytest.raises(StoreError, match="qa source"):
        store.qa_add(item["id"], "q", "a", "research")
    with pytest.raises(ItemNotFound):
        store.qa_add("item_nope", "q", "a", "seller")


def test_qa_add_requires_both_halves(store) -> None:
    item = store.create_item(title="Lamp", list_price=80.0)
    with pytest.raises(StoreError, match="non-empty"):
        store.qa_add(item["id"], "  ", "an answer", "seller")
    with pytest.raises(StoreError, match="non-empty"):
        store.qa_add(item["id"], "a question", "", "seller")


# --- selector cache ----------------------------------------------------------------------------


def _record(store, **overrides):
    fields = dict(
        market="carousell",
        flow="reply",
        step="message_box",
        strategy="css",
        query="textarea[name=message]",
        page_url_pattern=r"/inbox/\d+",
        action_kind="type",
    )
    fields.update(overrides)
    return store.ui_cache_record(**fields)


def test_a_recorded_selector_reads_back_fresh(store) -> None:
    _record(store)
    got = store.ui_cache_get("carousell", "reply", "message_box")
    assert got["hit"] is True and got["stale"] is False
    assert got["selector"]["query"] == "textarea[name=message]"
    assert got["selector"]["ok_streak"] == 1 and got["selector"]["fail_count"] == 0


def test_a_miss_is_a_hitless_stale_answer(store) -> None:
    got = store.ui_cache_get("carousell", "reply", "nothing_here")
    assert got["hit"] is False and got["stale"] is True and got["selector"] is None


def test_the_batched_read_returns_the_whole_flow(store) -> None:
    _record(store, step="message_box")
    _record(store, step="send_button", action_kind="click", query="button[type=submit]")
    got = store.ui_cache_get("carousell", "reply")
    assert set(got["steps"]) == {"message_box", "send_button"}
    assert got["hit"] is True
    assert all(entry["stale"] is False for entry in got["steps"].values())


def test_re_recording_a_working_step_grows_its_streak(store) -> None:
    _record(store)
    _record(store)
    assert store.ui_cache_get("carousell", "reply", "message_box")["selector"]["ok_streak"] == 2


def test_a_heal_re_record_clears_the_failures_and_restarts_the_streak(store) -> None:
    """The streak counts *consecutive* verified resolves, so a failure in between ends it — a healed
    selector starts earning trust again from one, not from where the drifted one left off."""
    _record(store)
    _record(store)
    store.ui_cache_fail("carousell", "reply", "message_box")
    _record(store, query="textarea.msg")
    entry = store.ui_cache_get("carousell", "reply", "message_box")["selector"]
    assert entry["query"] == "textarea.msg"
    assert entry["fail_count"] == 0  # the row just worked
    assert entry["ok_streak"] == 1


def test_record_refuses_a_row_with_no_page_guard(store) -> None:
    with pytest.raises(StoreError, match="page_url_pattern"):
        _record(store, page_url_pattern="")
    assert store.ui_cache_get("carousell", "reply", "message_box")["hit"] is False


def test_record_refuses_an_unknown_strategy_or_empty_query(store) -> None:
    with pytest.raises(StoreError, match="strategy"):
        _record(store, strategy="xpath")
    with pytest.raises(StoreError, match="non-empty query"):
        _record(store, query="   ")


def test_three_failures_make_a_selector_stale(store) -> None:
    _record(store)
    for _ in range(2):
        store.ui_cache_fail("carousell", "reply", "message_box")
    assert store.ui_cache_get("carousell", "reply", "message_box")["stale"] is False
    third = store.ui_cache_fail("carousell", "reply", "message_box")
    assert third["fail_count"] == 3
    assert store.ui_cache_get("carousell", "reply", "message_box")["stale"] is True


def test_invalidate_drops_a_step_or_the_whole_flow(store) -> None:
    _record(store, step="message_box")
    _record(store, step="send_button", query="button")
    assert store.ui_cache_invalidate("carousell", "reply", "message_box")["removed"] == 1
    assert store.ui_cache_get("carousell", "reply", "message_box")["hit"] is False
    assert store.ui_cache_invalidate("carousell", "reply")["removed"] == 1  # the survivor
    assert store.ui_cache_get("carousell", "reply")["hit"] is False


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"fail_count": 0, "page_url_pattern": "/x", "last_verified_at": 1000.0}, False),
        ({"fail_count": 3, "page_url_pattern": "/x", "last_verified_at": 1000.0}, True),
        ({"fail_count": 0, "page_url_pattern": "", "last_verified_at": 1000.0}, True),
        ({"fail_count": 0, "page_url_pattern": "/x", "last_verified_at": None}, True),
    ],
)
def test_the_staleness_predicate_covers_every_axis(entry, expected) -> None:
    assert ui_cache_is_stale(entry, 1000.0) is expected


def test_a_selector_ages_out_after_the_freshness_window() -> None:
    entry = {"fail_count": 0, "page_url_pattern": "/x", "last_verified_at": 0.0}
    assert ui_cache_is_stale(entry, 29 * _DAY) is False
    assert ui_cache_is_stale(entry, 31 * _DAY) is True


def test_the_cache_never_stores_a_value_or_an_address(store) -> None:
    """The cache is a DOM-locating hint layer; a price or address in it would be a leak with no
    reason to exist, so the columns for one simply aren't there."""
    _record(store)
    columns = {
        row["name"] for row in store._db.query("SELECT name FROM pragma_table_info('ui_cache')")
    }
    assert columns == {
        "market",
        "flow",
        "step",
        "strategy",
        "query",
        "action_kind",
        "page_url_pattern",
        "recorded_at",
        "last_verified_at",
        "last_ok_at",
        "fail_count",
        "ok_streak",
    }


# --- the daemon's inbound writer ----------------------------------------------------------------


def _sell_thread(store, tid="carousell:1"):
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    store.create_thread(
        thread_id=tid,
        side="sell",
        market="carousell",
        counterpart_handle="bob",
        item_id=item["id"],
    )
    return item


def test_record_inbound_writes_a_row_with_its_verdict(store) -> None:
    _sell_thread(store)
    assert store.record_inbound(
        "carousell:1", msg_id="m1", text="still available?", ts=100.0, scam_verdict="clean"
    )
    message = store.get_thread("carousell:1")["messages"][0]
    assert message["dir"] == "in" and message["scam_verdict"] == "clean"
    assert message["source"] == "marketplace"


def test_re_reading_the_same_tail_inserts_nothing_new(store) -> None:
    """Reconcile runs every tick over the same bubbles; the UNIQUE constraint — not a memo — is
    what makes a re-read a no-op."""
    _sell_thread(store)
    assert store.record_inbound("carousell:1", msg_id="m1", text="hi", ts=100.0) is True
    assert store.record_inbound("carousell:1", msg_id="m1", text="hi", ts=100.0) is False
    assert store.get_thread("carousell:1")["message_count"] == 1


def test_an_outbound_bubble_we_did_not_write_is_recorded_as_manual(store) -> None:
    _sell_thread(store)
    store.record_inbound(
        "carousell:1", msg_id="m2", text="posting today", ts=200.0, direction="out"
    )
    message = store.get_thread("carousell:1")["messages"][0]
    assert message["dir"] == "out" and message["source"] == "manual"


def test_record_inbound_never_advances_the_reply_cursor(store) -> None:
    """The cursor is the reply lane's only source of truth, so only a committed reply may move it —
    otherwise a crash between reading and answering would leave the buyer silently 'handled'."""
    _sell_thread(store)
    store.record_inbound("carousell:1", msg_id="m1", text="hi", ts=100.0)
    thread = store.get_thread("carousell:1")
    assert thread["cursor_last_msg_id"] is None and thread["cursor_last_ts"] is None


def test_record_inbound_on_an_unknown_thread_raises(store) -> None:
    with pytest.raises(ThreadNotFound):
        store.record_inbound("carousell:nope", msg_id="m1", text="hi")


# --- the reply lane -----------------------------------------------------------------------------


def _waiting_thread(store, tid="carousell:1", *, ts=100.0):
    item = _sell_thread(store, tid)
    store.record_inbound(tid, msg_id="m1", text="still available?", ts=ts)
    return item


def test_unhandled_inbound_lists_a_waiting_sell_thread(store) -> None:
    item = _waiting_thread(store)
    assert store.threads_with_unhandled_inbound() == [
        {"thread_id": "carousell:1", "item_id": item["id"]}
    ]


def _advance_cursor(store, thread_id, ts) -> None:
    # The cursor is advanced by the send bracket in production; a store test arranges it directly.
    with store._db.transaction() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE threads SET cursor_last_ts = ?, cursor_last_msg_id = 'm1' WHERE thread_id = ?",
            (ts, thread_id),
        )


def test_inbound_behind_the_cursor_is_handled(store) -> None:
    _waiting_thread(store, ts=100.0)
    _advance_cursor(store, "carousell:1", 200.0)
    assert store.threads_with_unhandled_inbound() == []


def test_terminal_held_and_buy_threads_are_excluded(store) -> None:
    _waiting_thread(store, "carousell:live")
    _waiting_thread(store, "carousell:closed")
    store.update_thread("carousell:closed", {"status": "closed"})
    _waiting_thread(store, "carousell:held")
    store.hold_thread("carousell:held", reason="scam")
    want = store.create_want(query="lamp")
    store.create_thread(
        thread_id="carousell:buy",
        side="buy",
        market="carousell",
        counterpart_handle="alice",
        want_id=want["want_id"],
    )
    store.record_inbound("carousell:buy", msg_id="b1", text="hi", ts=100.0)
    assert [r["thread_id"] for r in store.threads_with_unhandled_inbound()] == ["carousell:live"]


def test_an_open_escalation_excludes_the_thread(store) -> None:
    _waiting_thread(store)
    esc = store.escalate("carousell:1", open_question="what's your floor?")
    assert store.threads_with_unhandled_inbound() == []
    store.resolve_escalation(esc["id"], resolution="80")
    store.update_thread("carousell:1", {"status": "active"})
    assert [r["thread_id"] for r in store.threads_with_unhandled_inbound()] == ["carousell:1"]


def test_enqueue_reply_pass_claims_scope_and_coalesces(store) -> None:
    item = _waiting_thread(store)
    claimed = store.enqueue_reply_pass()
    assert claimed["thread_ids"] == ["carousell:1"] and claimed["item_ids"] == [item["id"]]
    assert store.enqueue_reply_pass() is None  # one in flight coalesces the rest
    store.claim_queued_pass()
    store.finish_pass(claimed["pass_id"], status="done", rc=0, cls="ok", summary="ok")
    # the buyer is still past the cursor, so the next lane tick re-enqueues
    assert store.enqueue_reply_pass()["thread_ids"] == ["carousell:1"]


def test_enqueue_reply_pass_is_none_with_nothing_waiting(store) -> None:
    assert store.enqueue_reply_pass() is None


def test_active_passes_of_types_reports_queued_and_running_with_payloads(store) -> None:
    _waiting_thread(store)
    store.enqueue_reply_pass()
    store.enqueue_pass("publish", {"item_id": "item_1", "market": "carousell"})
    store.enqueue_pass("channel", {"inbox_ids": [1]})

    active = store.active_passes_of_types(("reply", "publish"))
    assert {row["type"] for row in active} == {"reply", "publish"}
    publish = next(row for row in active if row["type"] == "publish")
    assert publish["payload"]["market"] == "carousell"
    assert store.active_passes_of_types(()) == []
