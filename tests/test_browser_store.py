"""Store accessors the browser layer adds: the Q&A bank, the selector cache and its staleness
predicate, the daemon's own inbound writer, and the reply lane's unhandled-inbound query +
coalescing enqueue."""

from __future__ import annotations

import pytest

from sellee.store import ItemNotFound, StoreError, ThreadNotFound, ui_cache_is_stale

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


def test_qa_search_ranks_on_the_query_and_caps(store) -> None:
    """The query orders the bank; it never removes a row. Filtering here can only hide the entry
    that answers the buyer, and the model — which does the matching — never sees what was
    dropped."""
    item = store.create_item(title="Lamp", list_price=80.0)
    store.qa_add(item["id"], "Any chips?", "One on the base.", "seller")
    store.qa_add(item["id"], "Bulb included?", "Yes.", "seller")

    assert [r["question"] for r in store.qa_search(item["id"], "chip")][0] == "Any chips?"
    # a match on the answer ranks too
    assert [r["question"] for r in store.qa_search(item["id"], "base")][0] == "Any chips?"
    # …and the row that did not match is still returned, in both cases
    assert len(store.qa_search(item["id"], "chip")) == 2
    assert len(store.qa_search(item["id"], "%")) == 2  # a query with no words ranks nothing
    assert len(store.qa_search(item["id"], limit=1)) == 1


def test_qa_add_rejects_a_non_seller_source_and_a_missing_item(store) -> None:
    item = store.create_item(title="Lamp", list_price=80.0)
    with pytest.raises(StoreError, match="qa source"):
        store.qa_add(item["id"], "q", "a", "research")
    with pytest.raises(ItemNotFound):
        store.qa_add("item_nope", "q", "a", "seller")
    with pytest.raises(StoreError, match="non-empty"):
        store.qa_add(item["id"], "  ", "an answer", "seller")


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
        # the freshness window, either side of it
        ({"fail_count": 0, "page_url_pattern": "/x", "last_verified_at": -29 * _DAY}, False),
        ({"fail_count": 0, "page_url_pattern": "/x", "last_verified_at": -31 * _DAY}, True),
    ],
)
def test_the_staleness_predicate_covers_every_axis(entry, expected) -> None:
    assert ui_cache_is_stale(entry, 1000.0) is expected


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
        {
            "thread_id": "carousell:1",
            "item_id": item["id"],
            "market": "carousell",
            # the message the buyer is waiting on — what a report about the wait is keyed to
            "waiting_on_msg_id": "m1",
            "waiting_since_ts": 100.0,
        }
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


def _agent_reply(store, thread_id, *, ts, msg_id="out|intent_x") -> None:
    with store._db.transaction() as conn:  # noqa: SLF001 — arranging what a send bracket writes
        conn.execute(
            "INSERT INTO thread_messages (thread_id, msg_id, dir, text, ts, source) "
            "VALUES (?, ?, 'out', 'sure!', ?, 'agent')",
            (thread_id, msg_id, ts),
        )


def _manual_reply(store, thread_id, *, ts, msg_id="out|abc123|1") -> None:
    with store._db.transaction() as conn:  # noqa: SLF001 — arranging what a page read writes
        conn.execute(
            "INSERT INTO thread_messages (thread_id, msg_id, dir, text, ts, source) "
            "VALUES (?, ?, 'out', 'i replied myself', ?, 'manual')",
            (thread_id, msg_id, ts),
        )


# The four situations the eligibility rule has to tell apart. The third is the one that matters:
# direction alone cannot distinguish it from the fourth, and reading it as "someone answered" leaves
# a buyer who asked something mid-reply waiting forever.


def test_a_buyer_waiting_on_us_is_eligible(store) -> None:
    _waiting_thread(store, ts=100.0)
    assert [r["thread_id"] for r in store.threads_with_unhandled_inbound()] == ["carousell:1"]


def test_our_own_reply_with_nothing_new_since_is_not_eligible(store) -> None:
    _waiting_thread(store, ts=100.0)
    _agent_reply(store, "carousell:1", ts=110.0)
    _advance_cursor(store, "carousell:1", 100.0)  # the send advanced it over what it answered
    assert store.threads_with_unhandled_inbound() == []


def test_a_buyer_who_wrote_again_after_our_reply_stays_eligible(store) -> None:
    """The message arrived while the pass was composing, so the reply that went out never saw it.
    Our own voice being last must not read as "answered" — nothing else will pick this up."""
    _waiting_thread(store, ts=100.0)
    store.record_inbound("carousell:1", msg_id="m2", text="how about $85?", ts=105.0)
    _agent_reply(store, "carousell:1", ts=110.0)
    _advance_cursor(store, "carousell:1", 100.0)  # only as far as the pass was given
    assert [r["thread_id"] for r in store.threads_with_unhandled_inbound()] == ["carousell:1"]


def test_a_reply_the_seller_typed_themselves_is_not_talked_over(store) -> None:
    _waiting_thread(store, ts=100.0)
    _manual_reply(store, "carousell:1", ts=110.0)
    assert store.threads_with_unhandled_inbound() == []


def test_a_buyer_writing_again_after_the_sellers_own_reply_is_eligible(store) -> None:
    """The seller answered, then the buyer came back. That is a fresh question for us."""
    _waiting_thread(store, ts=100.0)
    _manual_reply(store, "carousell:1", ts=110.0)
    store.record_inbound("carousell:1", msg_id="m2", text="and the charger?", ts=120.0)
    assert [r["thread_id"] for r in store.threads_with_unhandled_inbound()] == ["carousell:1"]


def test_an_open_escalation_excludes_the_thread(store) -> None:
    _waiting_thread(store)
    esc = store.escalate("carousell:1", open_question="what's your floor?")
    assert store.threads_with_unhandled_inbound() == []
    store.resolve_escalation(esc["id"], resolution="80")
    store.update_thread("carousell:1", {"status": "active"})
    assert [r["thread_id"] for r in store.threads_with_unhandled_inbound()] == ["carousell:1"]


def test_enqueue_reply_pass_claims_scope_and_coalesces(store) -> None:
    assert store.enqueue_reply_pass() is None  # nothing waiting
    item = _waiting_thread(store)
    claimed = store.enqueue_reply_pass()
    assert claimed["thread_ids"] == ["carousell:1"] and claimed["item_ids"] == [item["id"]]
    assert store.enqueue_reply_pass() is None  # one in flight coalesces the rest
    store.claim_queued_pass()
    store.finish_pass(claimed["pass_id"], status="done", rc=0, cls="ok", summary="ok")
    # the buyer is still past the cursor, so the next lane tick re-enqueues
    assert store.enqueue_reply_pass()["thread_ids"] == ["carousell:1"]


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


# --- holds on the one shared tab -------------------------------------------------------------


def test_a_hold_makes_the_browser_busy_and_releasing_frees_it(store) -> None:
    assert store.browser_hold_reason() == ""
    store.hold_browser("signin", "signing in to fb", 900.0)
    assert store.browser_hold_reason() == "signing in to fb"
    store.release_browser_hold("signin")
    assert store.browser_hold_reason() == ""


def test_re_claiming_renews_rather_than_stacking(store) -> None:
    """The installer takes one hold and renews it; a second row would outlive the phase by a
    full TTL."""
    store.hold_browser("setup", "signing in to marketplaces", 900.0)
    store.hold_browser("setup", "signing in to marketplaces", 900.0)
    store.release_browser_hold("setup")
    assert store.browser_hold_reason() == ""


def test_an_expired_hold_reads_as_free(store) -> None:
    store.hold_browser("signin", "signing in to fb", ttl_sec=-1.0)
    assert store.browser_hold_reason() == ""


def test_holders_release_independently(store) -> None:
    """A sign-in finishing inside an install must not hand the tab back; the next marketplace
    opens in it."""
    store.hold_browser("setup", "signing in to marketplaces", 900.0)
    store.hold_browser("signin", "signing in to fb", 900.0)
    store.release_browser_hold("signin")
    assert store.browser_hold_reason() == "signing in to marketplaces"


def test_releasing_a_hold_nobody_holds_is_a_success(store) -> None:
    store.release_browser_hold("signin")
    assert store.browser_hold_reason() == ""


# --- what a conversation is about ------------------------------------------------------------


def test_a_lookup_is_remembered_and_read_back(store) -> None:
    assert store.thread_listing_lookup("fb:1") is None
    store.record_thread_listing("fb:1", "fb", "9987", "Dyson␟still available?")
    assert store.thread_listing_lookup("fb:1") == {
        "product_id": "9987",
        "row_key": "Dyson␟still available?",
    }


def test_finding_nothing_is_remembered_too(store) -> None:
    """Remembering "none of ours" stops the conversation being re-opened every sweep to
    re-derive the same nothing."""
    store.record_thread_listing("fb:2", "fb", "", "Kettle␟is this still up?")
    assert store.thread_listing_lookup("fb:2") == {
        "product_id": "",
        "row_key": "Kettle␟is this still up?",
    }


def test_looking_again_replaces_rather_than_duplicates(store) -> None:
    store.record_thread_listing("fb:3", "fb", "", "a␟b")
    store.record_thread_listing("fb:3", "fb", "555", "a␟c")
    assert store.thread_listing_lookup("fb:3") == {"product_id": "555", "row_key": "a␟c"}


def test_adopting_a_listing_forgets_that_markets_lookups(store) -> None:
    """A new item can turn "none of ours" into a match, so remembered answers stop being answers;
    same transaction as the adoption."""
    store.record_thread_listing("fb:4", "fb", "", "a␟b")
    store.record_thread_listing("carousell:5", "carousell", "", "c␟d")
    store.record_survey_result(
        "fb",
        [{"listing_id": "77", "url": "https://fb/77", "title": "Fan", "price": 10.0}],
    )
    store.decide_discovered_listings("fb", decision="manage", manage="relist")

    store.adopt_discovered_listing(
        "fb", "77", title="Fan", list_price=10.0, currency="SGD", url="https://fb/77"
    )

    assert store.thread_listing_lookup("fb:4") is None
    # Scoped to the market that changed — Carousell learned nothing from a Facebook adoption.
    assert store.thread_listing_lookup("carousell:5") is not None


def test_linking_an_existing_item_forgets_that_markets_lookups(store) -> None:
    """The survey's twin-linking gives an item a market URL outside adoption entirely, and that URL
    is what joins a buyer on that listing to that item. A remembered "none of ours" surviving it is
    a buyer nobody ever looks at again."""
    item = store.create_item(title="Fan", list_price=10.0, currency="SGD")
    store.record_thread_listing("fb:6", "fb", "", "Fan")
    store.record_thread_listing("carousell:7", "carousell", "", "Fan")

    store.record_listing_url(item["id"], "fb", "https://fb/77")

    assert store.thread_listing_lookup("fb:6") is None
    assert store.thread_listing_lookup("carousell:7") is not None


# --- saying "these buyers are about listings I don't manage", once ----------------------------


def _texts(store):
    return [n["text"] for n in store.list_queued_notices()]


def test_buyers_we_cannot_place_are_reported_once(store) -> None:
    assert store.report_unplaceable_once("fb", "9 people are messaging you") is True
    assert store.report_unplaceable_once("fb", "21 people are messaging you") is False
    assert _texts(store) == ["9 people are messaging you"]


def test_each_market_is_reported_on_its_own(store) -> None:
    store.report_unplaceable_once("fb", "on Facebook")
    assert store.report_unplaceable_once("carousell", "on Carousell") is True
    assert _texts(store) == ["on Facebook", "on Carousell"]


def test_a_fresh_look_lets_it_be_said_again(store) -> None:
    """The notice tells the seller to ask for a fresh look at their listings; that ask is the one
    thing that un-mutes it."""
    store.report_unplaceable_once("fb", "first")

    store.reopen_market_survey("fb")

    assert store.report_unplaceable_once("fb", "second") is True
    assert _texts(store) == ["first", "second"]


def test_a_fresh_look_at_one_market_leaves_the_others_muted(store) -> None:
    store.report_unplaceable_once("fb", "on Facebook")
    store.report_unplaceable_once("carousell", "on Carousell")

    store.reopen_market_survey("fb")

    assert store.report_unplaceable_once("carousell", "again") is False


# --- whether the market's own listings are still an open question ------------------------------


def test_a_market_nobody_surveyed_is_settled(store) -> None:
    """A market nobody asked about must not read as one still being asked about — a market no
    adapter can survey would be muted forever."""
    assert store.market_survey_unsettled("fb") is False


def test_a_survey_owed_a_look_is_unsettled(store) -> None:
    store.request_market_survey("fb")
    assert store.market_survey_unsettled("fb") is True


def test_listings_the_seller_has_not_answered_about_are_unsettled(store) -> None:
    store.record_survey_result(
        "fb", [{"listing_id": "77", "url": "https://fb/77", "title": "Fan", "price": 10.0}]
    )
    assert store.market_survey_unsettled("fb") is True

    store.decide_discovered_listings("fb", decision="decline")
    assert store.market_survey_unsettled("fb") is False


def test_an_accepted_listing_still_being_adopted_is_unsettled(store) -> None:
    """The item does not exist yet, so the conversation about it would still read as unplaceable."""
    store.record_survey_result(
        "fb", [{"listing_id": "77", "url": "https://fb/77", "title": "Fan", "price": 10.0}]
    )
    store.decide_discovered_listings("fb", decision="manage", manage="relist")
    assert store.market_survey_unsettled("fb") is True

    store.adopt_discovered_listing(
        "fb", "77", title="Fan", list_price=10.0, currency="SGD", url="https://fb/77"
    )
    assert store.market_survey_unsettled("fb") is False


def test_a_market_given_up_on_is_settled(store) -> None:
    """Abandoned is an answer: we will never know what is listed there, and waiting on it would
    mute the report for good."""
    store.request_market_survey("fb")
    store.abandon_market_survey("fb")
    assert store.market_survey_unsettled("fb") is False


def test_settledness_is_per_market(store) -> None:
    store.request_market_survey("fb")
    assert store.market_survey_unsettled("carousell") is False
