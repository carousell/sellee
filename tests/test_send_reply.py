"""The send_reply composition matrix: no-sink → no_send_path (nothing recorded); go → sent with a
deterministic msg_id, a pacing row, a transcript out-row, an advanced cursor, a committed intent;
blocked verdicts record nothing; a killed send leaves the intent for the sweep to fold as
unconfirmed + escalate (never re-sent); duplicate commit is a no-op; terminal/held/escalated
refusals per side; followup stamps the thread. Plus record_manual_reply and the sweep."""

from __future__ import annotations

from datetime import datetime

import pytest

from sellee import intent_sweep
from sellee.config import Config
from sellee.engines import pacing
from sellee.tools.registry import ToolError, dispatch

_FAST = Config(reply_delay_sec=(0, 0), interactive_reply_delay_sec=(0, 0))


class FakeSink:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.sends: list = []

    def send(self, thread, text, kind, intent_id):
        self.sends.append((thread["thread_id"], text, kind, intent_id))
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


def test_a_browser_that_cannot_start_returns_no_send_path_recording_nothing(
    make_ctx, store
) -> None:
    """The sink is acquired before the reserve on purpose: a send with no browser must spend no
    pacing slot and write no intent — a pending intent would have the sweep asking a human about a
    send that provably never happened."""
    from sellee.browser.client import BrowserUnavailable

    _sell_thread(store)
    ctx = make_ctx("attended", config=_FAST)

    def _no_browser():
        raise BrowserUnavailable("Chrome is not running on port 9222")

    ctx.reply_sink = _no_browser
    res = dispatch("send_reply", {"thread_id": "fb:1", "text": "hi"}, ctx)
    assert res["status"] == "no_send_path"
    assert "Chrome is not running" in res["detail"]  # the seller-facing reason rides along
    assert _intents(store) == [] and _pacing_rows(store) == []
    assert store.get_thread("fb:1")["messages"] == []


# --- go path ----------------------------------------------------------------------------------


def test_go_sends_commits_and_advances_cursor(make_ctx, store) -> None:
    _sell_thread(store)
    store.record_inbound("fb:1", msg_id="m7", text="still there?", ts=100.0)
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
    assert [m["dir"] for m in thread["messages"]] == ["in", "out"]
    assert thread["cursor_last_msg_id"] == "m7"  # cursor advanced over the handled inbound
    # the message's own time, never the send's: stamping "now" would sweep up anything the buyer
    # added while this reply was being written
    assert thread["cursor_last_ts"] == 100.0
    assert _intents(store)[0]["status"] == "committed"


def test_a_cursor_cannot_be_advanced_onto_a_message_that_does_not_exist(make_ctx, store) -> None:
    """An id no row has cannot place the cursor in time. Rather than leaving it where it was — which
    would leave the buyer waiting and earn them a second answer — it falls back to their newest."""
    _sell_thread(store)
    store.record_inbound("fb:1", msg_id="real", text="still there?", ts=100.0)
    ctx = make_ctx("attended", reply_sink=FakeSink(), config=_FAST)
    dispatch("send_reply", {"thread_id": "fb:1", "text": "yes!", "in_msg_id": "invented"}, ctx)
    thread = store.get_thread("fb:1")
    assert thread["cursor_last_msg_id"] == "real" and thread["cursor_last_ts"] == 100.0


def test_followup_kind_stamps_thread(make_ctx, store) -> None:
    _sell_thread(store)
    ctx = make_ctx("attended", reply_sink=FakeSink(), config=_FAST)
    dispatch("send_reply", {"thread_id": "fb:1", "text": "still keen?", "kind": "followup"}, ctx)
    thread = store.get_thread("fb:1")
    assert thread["last_followup_ts"] is not None and thread["followup_disposition"] == "sent"


# --- blocked verdicts record nothing ----------------------------------------------------------


def test_wait_verdict_records_no_second_intent(make_ctx, store) -> None:
    _sell_thread(store)
    capped = Config(max_actions_per_hour=1, reply_delay_sec=(0, 0))
    ctx = make_ctx("attended", reply_sink=FakeSink(), config=capped)
    first = dispatch("send_reply", {"thread_id": "fb:1", "text": "one"}, ctx)
    assert first["status"] == "sent"
    second = dispatch("send_reply", {"thread_id": "fb:1", "text": "two"}, ctx)
    assert second["status"] == "wait"
    assert len(_intents(store)) == 1  # the blocked reply created no intent
    assert len(_pacing_rows(store)) == 1


def test_a_capped_reply_says_plainly_that_the_buyer_did_not_get_it(make_ctx, store) -> None:
    """The 2026-08-27 report bug at its source. A `wait` verdict returns no error, so the only thing
    standing between it and "All sorted, message queued" is the result saying, in the result itself,
    that nothing was delivered and nothing was recorded to deliver later."""
    _sell_thread(store)
    store.record_inbound("fb:1", msg_id="in|q|1", text="still available?", ts=10.0)
    capped = Config(max_actions_per_hour=1, reply_delay_sec=(0, 0))
    # burn the marketplace's only slot elsewhere, so the reply to THIS buyer is the blocked one
    store.reserve_action(marketplace="fb", kind="reply", cfg=pacing.resolve(capped, (0, 0)))
    ctx = make_ctx("attended", reply_sink=FakeSink(), config=capped)

    blocked = dispatch("send_reply", {"thread_id": "fb:1", "text": "two"}, ctx)
    assert blocked["status"] == "wait"
    assert blocked["delivered"] == "no"
    assert blocked["retry_after_sec"] > 0
    assert "delay_sec" not in blocked  # nothing is being held on our behalf
    assert "intent_id" not in blocked  # and there is no queued thing to point at
    # the thread is still unanswered, which is the only mechanism that gets the buyer a reply
    assert [t["thread_id"] for t in store.threads_with_unhandled_inbound()] == ["fb:1"]


def test_every_send_result_says_whether_the_buyer_got_it(make_ctx, store) -> None:
    """`delivered` is the field the seller-facing report must key on, so it is on every return —
    including the two that look like success from the outside and are not."""
    _sell_thread(store)
    paused = make_ctx("attended", reply_sink=FakeSink(), config=_FAST)
    store.set_paused(True)
    assert dispatch("send_reply", {"thread_id": "fb:1", "text": "x"}, paused) == {
        "status": "paused",
        "delivered": "no",
        "thread_id": "fb:1",
    }
    store.set_paused(False)

    failed = dispatch(
        "send_reply",
        {"thread_id": "fb:1", "text": "x"},
        make_ctx("attended", reply_sink=FakeSink(fail=True), config=_FAST),
    )
    assert (failed["status"], failed["delivered"]) == ("send_failed", "no")

    unverified = dispatch(
        "send_reply",
        {"thread_id": "fb:1", "text": "y"},
        make_ctx("attended", reply_sink=UnverifiedSink(store), config=_FAST),
    )
    assert (unverified["status"], unverified["delivered"]) == ("send_unverified", "unknown")

    blocked = dispatch(
        "send_reply",
        {"thread_id": "fb:1", "text": "z"},
        make_ctx("attended", reply_sink=UnverifiedSink(store), config=_FAST),
    )
    assert (blocked["status"], blocked["delivered"]) == ("unverified_open", "no")


def test_a_holding_line_leaves_the_buyers_question_unanswered(make_ctx, store) -> None:
    """A holding line ("let me check and get right back to you") answers nothing, so it must not
    advance the cursor over the message it was sent about.

    This is how kenzojr, echen53 and ncwei were stranded on 2026-08-27: their holding line marked
    their offer handled, the seller's real answer was then dropped by the pacing cap, and
    `threads_with_unhandled_inbound` could never see them again. A real reply still advances it.
    """
    _sell_thread(store)
    store.record_inbound("fb:1", msg_id="in|offer|1", text="70 can?", ts=10.0)
    ctx = make_ctx("attended", reply_sink=FakeSink(), config=_FAST)

    holding = dispatch(
        "send_reply",
        {"thread_id": "fb:1", "text": "Let me check and get right back to you!", "kind": "holding"},
        ctx,
    )
    assert holding["delivered"] == "yes"  # the buyer did get the holding line
    assert store.get_thread("fb:1")["cursor_last_msg_id"] is None
    assert [t["thread_id"] for t in store.threads_with_unhandled_inbound()] == ["fb:1"]

    dispatch("send_reply", {"thread_id": "fb:1", "text": "best is $105", "kind": "reply"}, ctx)
    assert store.get_thread("fb:1")["cursor_last_msg_id"] == "in|offer|1"
    assert store.threads_with_unhandled_inbound() == []


def test_quiet_verdict_records_nothing_at_store(store) -> None:
    _sell_thread(store)
    cfg = pacing.resolve(Config(), quiet_hours=(1380, 480))  # default night window
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

    # the sweep folds it as unconfirmed + opens an escalation — never a re-send. Only once the lane
    # has actually looked for it: the ask is the last resort, so effort is the gate, not the clock.
    intent_id = _intents(store)[0]["intent_id"]
    assert intent_sweep.run_stale_intent_sweep(bus=bus, store=store, grace_sec=0) == []
    store.bump_verify_attempt(intent_id)
    store.bump_verify_attempt(intent_id)
    folded = intent_sweep.run_stale_intent_sweep(bus=bus, store=store, grace_sec=0)
    assert len(folded) == 1 and folded[0]["escalation_new"] is True
    assert _intents(store)[0]["status"] == "unconfirmed"
    assert store.get_thread("fb:1")["status"] == "escalated"
    assert len(sink.sends) == 1  # the send was never retried


class UnverifiedSink(FakeSink):
    """A sink whose page took the message but whose read-back failed — the real sink's
    sent-but-unconfirmed shape."""

    def __init__(self, store):
        super().__init__()
        self.store = store

    def send(self, thread, text, kind, intent_id):
        self.sends.append((thread["thread_id"], text, kind, intent_id))
        self.store.mark_intent_sent_unverified(intent_id)
        raise RuntimeError("read-back failed")


def test_an_unverified_send_reports_itself_and_blocks_the_thread(make_ctx, store) -> None:
    """send_failed means retrying is safe; a send the page took but we could not confirm is the
    opposite case, so it reports its own status — and a fresh send on the thread is refused at the
    reserve, not left to the caller's judgement."""
    _sell_thread(store)
    sink = UnverifiedSink(store)
    ctx = make_ctx("attended", reply_sink=sink, config=_FAST)
    res = dispatch("send_reply", {"thread_id": "fb:1", "text": "hi"}, ctx)
    assert res["status"] == "send_unverified"

    again = dispatch("send_reply", {"thread_id": "fb:1", "text": "hi again"}, ctx)
    assert again["status"] == "unverified_open"
    assert len(sink.sends) == 1  # the second call never reached the sink
    assert len(_intents(store)) == 1  # and minted no second intent
    assert len(_pacing_rows(store)) == 1  # nor a second pacing row


def test_the_reserve_guard_hands_over_to_the_escalation_it_bounds(make_ctx, store, bus) -> None:
    """The guard keys on sent_unverified only. Once the sweep folds it and escalates, the thread's
    own status is the gate — and resolving the escalation is the human's deliberate way back in,
    after which sending works again."""
    _sell_thread(store)
    ctx = make_ctx("attended", reply_sink=UnverifiedSink(store), config=_FAST)
    dispatch("send_reply", {"thread_id": "fb:1", "text": "hi"}, ctx)

    for _ in range(intent_sweep.MIN_VERIFY_ATTEMPTS):  # the lane looked and did not find it
        store.bump_verify_attempt(_intents(store)[0]["intent_id"])
    intent_sweep.run_stale_intent_sweep(bus=bus, store=store, grace_sec=0)
    assert _intents(store)[0]["status"] == "unconfirmed"  # the reserve guard no longer applies…
    with pytest.raises(ToolError, match="escalated"):  # …the escalated thread is the gate now
        dispatch("send_reply", {"thread_id": "fb:1", "text": "hi"}, ctx)

    escalation = store.list_open_escalations()[0]
    store.resolve_escalation(escalation["id"], "checked the app — it did not send")
    store.update_thread("fb:1", {"status": "active"})
    ctx2 = make_ctx("attended", reply_sink=FakeSink(), config=_FAST)
    res = dispatch("send_reply", {"thread_id": "fb:1", "text": "hi again"}, ctx2)
    assert res["status"] == "sent"


def test_duplicate_commit_is_a_noop(store) -> None:
    _sell_thread(store)
    cfg = pacing.resolve(_FAST, quiet_hours=(0, 0))
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
