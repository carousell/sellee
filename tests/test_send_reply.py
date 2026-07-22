"""The send_reply composition matrix: no-sink → no_send_path (nothing recorded); go → sent with a
deterministic msg_id, a pacing row, a transcript out-row, an advanced cursor, a committed intent;
blocked verdicts record nothing; a killed send leaves the intent for the sweep to fold as
unconfirmed + escalate (never re-sent); duplicate commit is a no-op; terminal/held/escalated
refusals per side; followup stamps the thread. Plus record_manual_reply and the sweep."""

from __future__ import annotations

from datetime import datetime

import pytest

from selly_agent import intent_sweep
from selly_agent.config import Config
from selly_agent.engines import pacing
from selly_agent.tools.registry import ToolError, dispatch

_FAST = Config(reply_delay_sec=(0, 0), interactive_reply_delay_sec=(0, 0), quiet_hours=(0, 0))


class FakeSink:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.sends: list = []

    def send(self, thread, text, kind):
        self.sends.append((thread["thread_id"], text, kind))
        if self.fail:
            raise RuntimeError("browser send blew up")


def _sell_thread(store, tid="fb:1"):
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    store.create_thread(
        thread_id=tid, side="sell", market="fb", counterpart_handle="bob", item_id=item["id"]
    )
    return item


def _pacing_rows(store, market="fb"):
    return store._db.query("SELECT ts FROM pacing_actions WHERE marketplace = ?", (market,))


def _intents(store):
    return store._db.query("SELECT intent_id, status FROM send_intents")


# --- no live sink -----------------------------------------------------------------------------


def test_no_sink_returns_no_send_path_recording_nothing(make_ctx, store) -> None:
    _sell_thread(store)
    ctx = make_ctx("attended", config=_FAST)  # no reply_sink
    res = dispatch("send_reply", {"thread_id": "fb:1", "text": "hi"}, ctx)
    assert res["status"] == "no_send_path"
    assert _intents(store) == [] and _pacing_rows(store) == []
    assert store.get_thread("fb:1")["messages"] == []


# --- go path ----------------------------------------------------------------------------------


def test_go_sends_commits_and_advances_cursor(make_ctx, store) -> None:
    _sell_thread(store)
    sink = FakeSink()
    ctx = make_ctx("attended", reply_sink=sink, config=_FAST)
    res = dispatch(
        "send_reply", {"thread_id": "fb:1", "text": "still available!", "in_msg_id": "m7"}, ctx
    )
    assert res["status"] == "sent"
    assert res["msg_id"] == f"out|{res['intent_id']}"  # deterministic id from the intent
    assert len(sink.sends) == 1
    assert len(_pacing_rows(store)) == 1  # a pacing row was recorded at reserve
    thread = store.get_thread("fb:1")
    assert [m["dir"] for m in thread["messages"]] == ["out"]
    assert thread["cursor_last_msg_id"] == "m7"  # cursor advanced over the handled inbound
    assert _intents(store)[0]["status"] == "committed"


def test_followup_kind_stamps_thread(make_ctx, store) -> None:
    _sell_thread(store)
    ctx = make_ctx("attended", reply_sink=FakeSink(), config=_FAST)
    dispatch("send_reply", {"thread_id": "fb:1", "text": "still keen?", "kind": "followup"}, ctx)
    thread = store.get_thread("fb:1")
    assert thread["last_followup_ts"] is not None and thread["followup_disposition"] == "sent"


# --- blocked verdicts record nothing ----------------------------------------------------------


def test_wait_verdict_records_no_second_intent(make_ctx, store) -> None:
    _sell_thread(store)
    capped = Config(max_actions_per_hour=1, reply_delay_sec=(0, 0), quiet_hours=(0, 0))
    ctx = make_ctx("attended", reply_sink=FakeSink(), config=capped)
    first = dispatch("send_reply", {"thread_id": "fb:1", "text": "one"}, ctx)
    assert first["status"] == "sent"
    second = dispatch("send_reply", {"thread_id": "fb:1", "text": "two"}, ctx)
    assert second["status"] == "wait"
    assert len(_intents(store)) == 1  # the blocked reply created no intent
    assert len(_pacing_rows(store)) == 1


def test_quiet_verdict_records_nothing_at_store(store) -> None:
    _sell_thread(store)
    cfg = pacing.resolve(Config(quiet_hours=(23, 8)))  # default night window
    two_am = datetime.fromisoformat("2026-07-22T02:00:00").timestamp()
    res = store.reserve_reply(
        thread_id="fb:1", kind="reply", text="x", in_msg_id=None, cfg=cfg, now=two_am
    )
    assert res["verdict"] == "quiet"
    assert _intents(store) == [] and _pacing_rows(store) == []


# --- crash healing ----------------------------------------------------------------------------


def test_sink_failure_leaves_pending_then_sweep_folds_unconfirmed(make_ctx, store, bus) -> None:
    _sell_thread(store)
    sink = FakeSink(fail=True)
    ctx = make_ctx("attended", reply_sink=sink, config=_FAST)
    res = dispatch("send_reply", {"thread_id": "fb:1", "text": "hi"}, ctx)
    assert res["status"] == "send_failed"
    assert _intents(store)[0]["status"] == "pending"  # left for the sweep, no commit
    assert store.get_thread("fb:1")["messages"] == []  # nothing folded to the transcript

    # the sweep (grace 0) folds it as unconfirmed + opens an escalation — never a re-send
    folded = intent_sweep.run_stale_intent_sweep(bus=bus, store=store, grace_sec=0)
    assert len(folded) == 1 and folded[0]["escalation_new"] is True
    assert _intents(store)[0]["status"] == "unconfirmed"
    assert store.get_thread("fb:1")["status"] == "escalated"
    assert len(sink.sends) == 1  # the send was never retried


def test_duplicate_commit_is_a_noop(store) -> None:
    _sell_thread(store)
    cfg = pacing.resolve(_FAST)
    reserved = store.reserve_reply(
        thread_id="fb:1", kind="reply", text="hi", in_msg_id="m1", cfg=cfg
    )
    store.commit_reply(
        intent_id=reserved["intent_id"], thread_id="fb:1", in_msg_id="m1", text="hi", kind="reply"
    )
    store.commit_reply(
        intent_id=reserved["intent_id"], thread_id="fb:1", in_msg_id="m1", text="hi", kind="reply"
    )
    assert store.get_thread("fb:1")["message_count"] == 1  # deterministic msg_id → single row


# --- refusals ---------------------------------------------------------------------------------


def test_terminal_sell_thread_refused(make_ctx, store) -> None:
    _sell_thread(store)
    store.update_thread("fb:1", {"status": "closed"})
    ctx = make_ctx("attended", reply_sink=FakeSink(), config=_FAST)
    with pytest.raises(ToolError, match="not eligible"):
        dispatch("send_reply", {"thread_id": "fb:1", "text": "hi"}, ctx)


def test_buy_agreed_allows_reply_but_refuses_followup(make_ctx, store) -> None:
    want = store.create_want(query="thing")
    store.create_thread(
        thread_id="cl:1", side="buy", market="cl", counterpart_handle="s", want_id=want["want_id"]
    )
    with store._db.transaction() as conn:
        conn.execute("UPDATE threads SET status='agreed' WHERE thread_id='cl:1'")
    ctx = make_ctx("attended", config=_FAST)  # no sink → a passing refusal reaches no_send_path
    reply = dispatch("send_reply", {"thread_id": "cl:1", "text": "on my way"}, ctx)
    assert reply["status"] == "no_send_path"  # a reply on an agreed buy thread is allowed
    with pytest.raises(ToolError, match="not eligible"):
        dispatch(
            "send_reply", {"thread_id": "cl:1", "text": "still there?", "kind": "followup"}, ctx
        )


# --- manual reply -----------------------------------------------------------------------------


def test_record_manual_reply_no_cursor_advance_and_dedups(make_ctx, store) -> None:
    _sell_thread(store)
    ctx = make_ctx("attended")
    first = dispatch("record_manual_reply", {"thread_id": "fb:1", "text": "I replied by hand"}, ctx)
    assert first["recorded"] is True
    dup = dispatch(
        "record_manual_reply", {"thread_id": "fb:1", "text": "I  replied by   hand"}, ctx
    )
    assert dup["deduped"] is True  # normalized-text dedup
    thread = store.get_thread("fb:1")
    assert thread["message_count"] == 1
    assert thread["cursor_last_msg_id"] is None  # a manual record never advances the cursor
