"""The channel/needs-me store accessors: channel state, durable inbox, notices, pause control.

These are the deterministic substrate the poller, drain lane, and catchup all build on, so they
are pinned directly (the poller/transport tests layer on top). The store is exercised over a
freshly-migrated sellee.db via the shared `store` fixture.
"""

from __future__ import annotations

import time

import pytest

from sellee.store import BIND_NONCE_TTL_SEC, StoreError, bind_nonce_live

# --- channel state: off -> awaiting-bind -> bound -------------------------------------------


def test_channel_defaults_when_no_row(store) -> None:
    ch = store.get_channel()
    assert ch["chat_id"] is None
    assert ch["bind_nonce"] is None
    assert ch["update_offset"] == 0
    assert ch["adapter"] == "telegram"


def test_arm_bind_sets_nonce_and_clears_chat(store) -> None:
    store.arm_bind("sellee_bot", "nonce-1")
    ch = store.get_channel()
    assert ch["bot_username"] == "sellee_bot"
    assert ch["bind_nonce"] == "nonce-1"
    assert ch["chat_id"] is None
    assert ch["update_offset"] == 0


def test_arm_bind_gives_the_nonce_a_deadline_by_default(store) -> None:
    """Arming without a deadline is impossible rather than merely discouraged — a nonce with no
    expiry is adoptable forever, which is the whole failure the column exists for."""
    before = time.time()
    store.arm_bind("sellee_bot", "nonce-1")
    expires_ts = store.get_channel()["bind_nonce_expires_ts"]
    assert expires_ts is not None
    assert before + BIND_NONCE_TTL_SEC <= expires_ts <= time.time() + BIND_NONCE_TTL_SEC
    assert bind_nonce_live(store.get_channel()) is True


def test_bind_nonce_live_reads_a_missing_deadline_as_expired(store) -> None:
    """A row armed before the TTL existed carries NULL here. Failing closed retires it; failing
    open would leave exactly the unbounded nonce this replaced."""
    store.arm_bind("sellee_bot", "nonce-1")
    with store._db.transaction() as conn:
        conn.execute("UPDATE channel SET bind_nonce_expires_ts = NULL WHERE id = 1")
    assert bind_nonce_live(store.get_channel()) is False


def test_clear_bind_nonce_retires_the_nonce_and_keeps_the_cursor(store) -> None:
    store.arm_bind("sellee_bot", "nonce-1")
    store.advance_offset(7)  # pre-bind traffic was acked before the window closed
    store.clear_bind_nonce("nonce-1")
    ch = store.get_channel()
    assert ch["bind_nonce"] is None
    assert ch["bind_nonce_expires_ts"] is None
    assert ch["update_offset"] == 7  # already-acked updates must not be re-offered
    assert ch["bot_username"] == "sellee_bot"


def test_clear_bind_nonce_spares_a_nonce_armed_after_the_read(store) -> None:
    """The retire callers decide from a row read at tick start. A seller re-running connect
    between that read and the clear re-arms the row; the stale retire must not eat the fresh
    nonce and leave the seller waiting out a bind that can never arrive."""
    store.arm_bind("sellee_bot", "nonce-old")
    store.arm_bind("sellee_bot", "nonce-new")  # the re-run, between read and clear
    store.clear_bind_nonce("nonce-old")
    ch = store.get_channel()
    assert ch["bind_nonce"] == "nonce-new"
    assert bind_nonce_live(ch) is True


def test_complete_bind_atomic_chat_nonce_cursor(store) -> None:
    store.arm_bind("sellee_bot", "nonce-1")
    store.complete_bind(555, update_offset=42, nonce=store.get_channel()["bind_nonce"])
    ch = store.get_channel()
    assert ch["chat_id"] == 555
    assert ch["bind_nonce"] is None  # single-use, cleared on bind
    assert ch["bind_nonce_expires_ts"] is None  # and its deadline with it
    assert ch["update_offset"] == 42
    assert ch["bound_ts"] is not None


def test_complete_bind_refuses_a_nonce_a_re_arm_retired(store) -> None:
    """The caller matched the nonce against a row read earlier; a seller re-running connect in
    between re-arms the row, and the retired nonce must not bind against the fresh one."""
    store.arm_bind("sellee_bot", "nonce-old")
    store.arm_bind("sellee_bot", "nonce-new")
    assert store.complete_bind(555, update_offset=1, nonce="nonce-old") is False
    ch = store.get_channel()
    assert ch["chat_id"] is None
    assert ch["bind_nonce"] == "nonce-new"


def test_complete_bind_refuses_a_nonce_past_its_deadline(store, monkeypatch) -> None:
    store.arm_bind("sellee_bot", "nonce-1")
    from sellee import store as store_mod

    monkeypatch.setattr(store_mod, "_now", lambda: time.time() + BIND_NONCE_TTL_SEC + 1)
    assert store.complete_bind(555, update_offset=1, nonce="nonce-1") is False
    assert store.get_channel()["chat_id"] is None


def test_complete_bind_without_arm_raises(store) -> None:
    with pytest.raises(StoreError):
        store.complete_bind(555, update_offset=1, nonce=store.get_channel()["bind_nonce"])


def test_rebind_same_bot_keeps_welcome_and_commands(store) -> None:
    store.arm_bind("sellee_bot", "n1")
    store.complete_bind(555, update_offset=1, nonce=store.get_channel()["bind_nonce"])
    store.stamp_welcomed()
    store.stamp_commands_hash("abc123")
    store.arm_bind("sellee_bot", "n2")  # re-connect, same bot
    ch = store.get_channel()
    assert ch["welcomed_at"] is not None  # never re-greet the same bot
    assert ch["commands_hash"] == "abc123"
    assert ch["chat_id"] is None and ch["bind_nonce"] == "n2"


def test_rebind_new_bot_resets_welcome_and_commands(store) -> None:
    store.arm_bind("sellee_bot", "n1")
    store.complete_bind(555, update_offset=1, nonce=store.get_channel()["bind_nonce"])
    store.stamp_welcomed()
    store.stamp_commands_hash("abc123")
    store.arm_bind("other_bot", "n2")  # a new token -> a new bot
    ch = store.get_channel()
    assert ch["welcomed_at"] is None  # the new bot must greet
    assert ch["commands_hash"] is None


def test_rebind_same_bot_different_adapter_resets_welcome_and_commands(store) -> None:
    store.arm_bind("sellee_bot", "n1")
    store.complete_bind(555, update_offset=1, nonce=store.get_channel()["bind_nonce"])
    store.stamp_welcomed()
    store.stamp_commands_hash("abc123")
    store.arm_bind("sellee_bot", "n2", adapter="discord")  # same bot, different adapter
    ch = store.get_channel()
    assert ch["welcomed_at"] is None  # switch to different adapter must reset
    assert ch["commands_hash"] is None
    assert ch["adapter"] == "discord"


def test_arm_bind_accepts_a_discord_adapter(store) -> None:
    store.arm_bind("SelleeBot", "nonce-1", adapter="discord")
    ch = store.get_channel()
    assert ch["adapter"] == "discord"


def test_arm_bind_still_defaults_to_telegram(store) -> None:
    store.arm_bind("SelleeBot", "nonce-1")
    ch = store.get_channel()
    assert ch["adapter"] == "telegram"


def test_arm_bind_refuses_an_unknown_adapter(store) -> None:
    """The column takes any non-empty string, so arm_bind is where an adapter name is checked —
    binding to a provider the daemon will never start would leave the seller waiting forever."""
    with pytest.raises(ValueError, match="unknown channel adapter"):
        store.arm_bind("SelleeBot", "nonce-1", adapter="whatsapp")
    assert store.get_channel()["bind_nonce"] is None  # nothing armed


def test_advance_offset_only_moves_forward(store) -> None:
    store.arm_bind("sellee_bot", "n1")
    store.complete_bind(555, update_offset=10, nonce=store.get_channel()["bind_nonce"])
    store.advance_offset(5)  # a lower value is ignored
    assert store.get_channel()["update_offset"] == 10
    store.advance_offset(20)
    assert store.get_channel()["update_offset"] == 20


# --- durable inbox: ingest, dedup, claim, fold ----------------------------------------------


def _ev(update_id, kind="text", text="hi", **payload):
    return {"event_id": update_id, "kind": kind, "text": text, "payload": payload, "src_ts": 1.0}


def test_ingest_inserts_rows_and_advances_cursor(store) -> None:
    store.arm_bind("b", "n")
    store.complete_bind(1, update_offset=100, nonce=store.get_channel()["bind_nonce"])
    inserted = store.ingest_updates([_ev(100), _ev(101)], update_offset=102)
    assert [r["event_id"] for r in inserted] == [100, 101]
    assert store.get_channel()["update_offset"] == 102
    assert store.count_pending_inbox() == 2


def test_ingest_dedups_by_event_id(store) -> None:
    store.arm_bind("b", "n")
    store.complete_bind(1, update_offset=0, nonce=store.get_channel()["bind_nonce"])
    store.ingest_updates([_ev(100)], update_offset=101)
    again = store.ingest_updates([_ev(100), _ev(101)], update_offset=102)
    assert [r["event_id"] for r in again] == [101]  # 100 already ingested
    assert store.count_pending_inbox() == 2


def test_fast_path_handled_rows_leave_pending(store) -> None:
    store.arm_bind("b", "n")
    store.complete_bind(1, update_offset=0, nonce=store.get_channel()["bind_nonce"])
    rows = store.ingest_updates(
        [_ev(1, kind="command", text="/pause"), _ev(2, text="hello")], update_offset=3
    )
    store.mark_inbox_handled([rows[0]["id"]], "fast_path")
    assert store.count_pending_inbox() == 1  # only the free-text row still routes


def test_enqueue_channel_pass_claims_and_folds(store) -> None:
    store.arm_bind("b", "n")
    store.complete_bind(1, update_offset=0, nonce=store.get_channel()["bind_nonce"])
    store.ingest_updates([_ev(1, text="a"), _ev(2, text="b")], update_offset=3)
    pass_id = store.enqueue_channel_pass()
    assert pass_id is not None
    claimed = store.inbox_for_pass(pass_id)
    assert [c["text"] for c in claimed] == ["a", "b"]
    assert store.count_pending_inbox() == 0  # claimed, not pending
    assert store.fold_inbox(pass_id, "handled") == 2


def test_enqueue_channel_pass_coalesces(store) -> None:
    store.arm_bind("b", "n")
    store.complete_bind(1, update_offset=0, nonce=store.get_channel()["bind_nonce"])
    store.ingest_updates([_ev(1, text="a")], update_offset=2)
    first = store.enqueue_channel_pass()
    assert first is not None
    # a channel pass is already queued: later arrivals do not spawn a second one
    store.ingest_updates([_ev(2, text="b")], update_offset=3)
    assert store.enqueue_channel_pass() is None
    assert store.count_pending_inbox() == 1  # the new row waits for the next pass


def test_enqueue_channel_pass_none_when_no_pending(store) -> None:
    assert store.enqueue_channel_pass() is None


def test_fold_inbox_rejects_bad_status(store) -> None:
    with pytest.raises(StoreError):
        store.fold_inbox("pass_x", "pending")


# --- transcript window: inbox + notices interleaved by local clock --------------------------


def test_recent_transcript_interleaves_by_local_clock(store, monkeypatch) -> None:
    import sellee.store as store_mod

    # Stamp each write at a distinct, increasing local time so the interleave is deterministic.
    ticks = iter([100.0, 200.0, 300.0])
    monkeypatch.setattr(store_mod, "_now", lambda: next(ticks))
    store.ingest_updates([_ev(1, text="buyer asks")], update_offset=2)  # in @100
    store.queue_notice("agent replies")  # out @200
    store.ingest_updates([_ev(2, text="yes do that")], update_offset=3)  # in @300
    window = store.recent_transcript(limit=10)
    assert [(e["direction"], e["text"]) for e in window] == [
        ("in", "buyer asks"),
        ("out", "agent replies"),
        ("in", "yes do that"),
    ]


def test_recent_transcript_carries_media_paths(store) -> None:
    """A photo's path has to outlive the pass that claimed its row: the listing flow spans passes,
    and the window is the only thing a later pass sees of an earlier turn."""
    photo = {
        "event_id": 1,
        "kind": "photo",
        "text": "list this",
        "payload": {},
        "src_ts": 1.0,
        "media_paths": ["/m/a.jpg", "/m/b.jpg"],
    }
    store.ingest_updates([photo, _ev(2, text="how much?")], update_offset=3)
    window = store.recent_transcript(limit=10)
    by_text = {e["text"]: e for e in window}
    assert by_text["list this"]["media_paths"] == ["/m/a.jpg", "/m/b.jpg"]
    assert by_text["how much?"]["media_paths"] == []  # a text row has none


def test_recent_transcript_caps_to_limit(store, monkeypatch) -> None:
    import itertools

    import sellee.store as store_mod

    clock = itertools.count(1.0, 1.0)  # a distinct, increasing local time per write
    monkeypatch.setattr(store_mod, "_now", lambda: next(clock))
    for i in range(1, 11):
        store.ingest_updates([_ev(i, text=f"m{i}")], update_offset=i + 1)
    window = store.recent_transcript(limit=3)
    assert len(window) == 3
    assert [e["text"] for e in window] == ["m8", "m9", "m10"]  # the most recent, oldest-first


# --- notices: queue -> drain / catchup ------------------------------------------------------


def test_queue_and_deliver_notice(store) -> None:
    nid = store.queue_notice("ping", ref="thread-1")
    queued = store.list_queued_notices()
    assert len(queued) == 1 and queued[0]["id"] == nid and queued[0]["ref"] == "thread-1"
    store.mark_notice_delivered(nid, "channel")
    assert store.list_queued_notices() == []
    assert store.count_queued_notices() == 0


def test_notice_attempts_bump_keeps_it_queued(store) -> None:
    nid = store.queue_notice("ping")
    assert store.bump_notice_attempts(nid) == 1
    assert store.bump_notice_attempts(nid) == 2
    assert store.count_queued_notices() == 1  # still visible, never silently dropped


def test_mark_notice_delivered_rejects_bad_via(store) -> None:
    nid = store.queue_notice("ping")
    with pytest.raises(StoreError):
        store.mark_notice_delivered(nid, "carrier-pigeon")


def test_claim_queued_notices_is_fifo(store) -> None:
    a = store.queue_notice("first")
    b = store.queue_notice("second")
    claimed = store.claim_queued_notices(limit=10)
    assert [n["id"] for n in claimed] == [a, b]


def test_queue_welcome_notices_queues_fifo_and_stamps_welcomed(store) -> None:
    store.arm_bind("sellee_bot", "n1")
    store.complete_bind(555, update_offset=1, nonce=store.get_channel()["bind_nonce"])
    store.queue_welcome_notices([("hello", None), ("list something", [("Skip", "skipcta")])])
    queued = store.list_queued_notices()
    assert [n["text"] for n in queued] == ["hello", "list something"]
    assert queued[0]["controls"] is None
    assert queued[1]["controls"] == [["Skip", "skipcta"]]
    assert store.get_channel()["welcomed_at"] is not None  # stamped in the same transaction


def test_has_notice_with_ref_spans_queued_and_delivered(store) -> None:
    assert store.has_notice_with_ref("first-listing-nudge") is False
    nid = store.queue_notice("nudge", ref="first-listing-nudge")
    assert store.has_notice_with_ref("first-listing-nudge") is True
    store.mark_notice_delivered(nid, "channel")
    assert store.has_notice_with_ref("first-listing-nudge") is True  # delivered still counts


# --- meta: the generic durable KV ------------------------------------------------------------


def test_meta_missing_key_reads_none(store) -> None:
    assert store.get_meta("first_listing_cta_skipped_ts") is None


def test_meta_set_and_overwrite(store) -> None:
    store.set_meta("first_listing_cta_skipped_ts", "123.0")
    assert store.get_meta("first_listing_cta_skipped_ts") == "123.0"
    store.set_meta("first_listing_cta_skipped_ts", "456.0")  # an upsert, not insert-only
    assert store.get_meta("first_listing_cta_skipped_ts") == "456.0"


# --- pause control: missing row reads as not paused -----------------------------------------


def test_pause_defaults_not_paused(store) -> None:
    assert store.is_paused() is False


def test_pause_and_resume(store) -> None:
    store.set_paused(True, source="telegram")
    assert store.is_paused() is True
    ch = store._db.query("SELECT since_ts, source FROM control WHERE id = 1")[0]
    assert ch["since_ts"] is not None and ch["source"] == "telegram"
    store.set_paused(False, source="telegram")
    assert store.is_paused() is False


def test_redundant_pause_keeps_since(store, monkeypatch) -> None:
    import sellee.store as store_mod

    ticks = iter([111.0, 222.0])
    monkeypatch.setattr(store_mod, "_now", lambda: next(ticks))
    store.set_paused(True, source="a")
    store.set_paused(True, source="b")  # a redundant pause keeps the original since_ts
    since = store._db.query("SELECT since_ts FROM control WHERE id = 1")[0]["since_ts"]
    assert since == 111.0
