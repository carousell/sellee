"""Store accessors: item CRUD with J7 field constraints, the hardened floor writer (ported
from the legacy floor_set discipline), and the single-flight pass queue."""

from __future__ import annotations

import concurrent.futures

import pytest

from sellee.store import (
    ItemNotFound,
    Store,
    StoreError,
    ThreadNotFound,
)


def _item(store: Store, **kw) -> dict:
    base = {"title": "Vintage lamp", "list_price": 80.0, "currency": "SGD"}
    base.update(kw)
    return store.create_item(**base)


def _want(store: Store, **kw) -> dict:
    base = {"query": "iPhone 15"}
    base.update(kw)
    return store.create_want(**base)


# --- items ------------------------------------------------------------------------------------


def test_create_and_get_item_never_returns_a_floor(store: Store) -> None:
    item = _item(store)
    assert item["id"].startswith("item_")
    assert item["status"] == "draft"
    assert item["listing_urls"] == {}
    fetched = store.get_item(item["id"])
    assert "floor" not in fetched
    assert fetched["title"] == "Vintage lamp"


def test_create_item_rejects_blank_title(store: Store) -> None:
    with pytest.raises(StoreError):
        store.create_item(title="   ", list_price=10.0)


def test_list_items_filters_by_status(store: Store) -> None:
    a = _item(store, title="A")
    _item(store, title="B")
    store.update_item(a["id"], {"status": "ready"})
    ready = store.list_items(status="ready")
    assert [i["id"] for i in ready] == [a["id"]]
    assert len(store.list_items()) == 2


def test_update_item_writable_fields(store: Store) -> None:
    item = _item(store)
    updated = store.update_item(item["id"], {"title": "New", "list_price": 90.0})
    assert updated["title"] == "New"
    assert updated["list_price"] == 90.0
    assert updated["updated_ts"] >= item["updated_ts"]


def test_update_item_rejects_listing_urls(store: Store) -> None:
    item = _item(store)
    with pytest.raises(StoreError, match="listing_urls"):
        store.update_item(item["id"], {"listing_urls": {"carousell-ai": "http://x"}})


def test_update_item_rejects_unknown_field(store: Store) -> None:
    item = _item(store)
    with pytest.raises(StoreError, match="unknown"):
        store.update_item(item["id"], {"published_at": 123})


def test_update_item_status_constrained_to_draft_ready(store: Store) -> None:
    item = _item(store)
    store.update_item(item["id"], {"status": "ready"})  # allowed
    with pytest.raises(StoreError, match="status"):
        store.update_item(item["id"], {"status": "sold"})


def test_update_missing_item_raises(store: Store) -> None:
    with pytest.raises(ItemNotFound):
        store.update_item("item_nope", {"title": "x"})


def test_record_listing_url_merges(store: Store) -> None:
    item = _item(store)
    r = store.record_listing_url(item["id"], "carousell-ai", "https://www.carousell.ai/listing/1")
    assert r["listing_urls"]["carousell-ai"] == "https://www.carousell.ai/listing/1"


# --- floors: ported floor_set discipline ------------------------------------------------------


def test_floor_ack_carries_no_value(store: Store) -> None:
    item = _item(store, list_price=80.0)
    ack = store.set_floor(item["id"], 63.0, "seller")
    # the exact dict is the guarantee: provenance and outcome only, no floor key or value
    assert ack == {"status": "written", "item_id": item["id"], "source": "seller", "replaced": None}
    assert "floor" not in ack
    stored = store.get_floor(item["id"])
    assert stored["floor"] == 63.0 and stored["source"] == "seller"


def test_floor_above_list_refused_nothing_written(store: Store) -> None:
    item = _item(store, list_price=80.0)
    with pytest.raises(StoreError, match="list price"):
        store.set_floor(item["id"], 85.0, "seller")
    assert store.get_floor(item["id"]) is None


def test_floor_at_list_allowed(store: Store) -> None:
    item = _item(store, list_price=80.0)
    store.set_floor(item["id"], 80.0, "seller")
    assert store.get_floor(item["id"])["floor"] == 80.0


@pytest.mark.parametrize("bad", [0, -5, True])
def test_floor_non_positive_or_bool_refused(store: Store, bad) -> None:
    item = _item(store)
    with pytest.raises(StoreError):
        store.set_floor(item["id"], bad, "seller")


def test_floor_unknown_source_refused(store: Store) -> None:
    item = _item(store)
    with pytest.raises(StoreError):
        store.set_floor(item["id"], 50.0, "wat")


def test_floor_missing_item_refused(store: Store) -> None:
    with pytest.raises(ItemNotFound):
        store.set_floor("item_nope", 50.0, "seller")


def test_floor_overwrite_rules(store: Store) -> None:
    item = _item(store, list_price=80.0)
    store.set_floor(item["id"], 80.0, "default")
    # seller overwrites a defaulted floor
    ack = store.set_floor(item["id"], 60.0, "seller")
    assert ack["replaced"] == "default"
    assert store.get_floor(item["id"])["floor"] == 60.0
    # overwriting a seller floor without force is refused
    with pytest.raises(StoreError, match="force"):
        store.set_floor(item["id"], 40.0, "seller")
    assert store.get_floor(item["id"])["floor"] == 60.0
    # a default write never clobbers a seller floor
    with pytest.raises(StoreError):
        store.set_floor(item["id"], 40.0, "default")
    assert store.get_floor(item["id"])["floor"] == 60.0
    # force updates the seller floor
    store.set_floor(item["id"], 40.0, "seller", force=True)
    assert store.get_floor(item["id"])["floor"] == 40.0


def test_default_rewrite_is_allowed(store: Store) -> None:
    item = _item(store, list_price=80.0)
    store.set_floor(item["id"], 80.0, "default")
    store.set_floor(item["id"], 80.0, "default")  # idempotent-ish, no refusal
    assert store.get_floor(item["id"])["source"] == "default"


def test_concurrent_seller_vs_default_never_loses_seller_floor(store: Store) -> None:
    for _ in range(6):
        item = _item(store, list_price=80.0)

        def seller(item_id=item["id"]):
            return store.set_floor(item_id, 60.0, "seller")

        def default(item_id=item["id"]):
            try:
                return store.set_floor(item_id, 80.0, "default")
            except StoreError:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            fs, fd = ex.submit(seller), ex.submit(default)
            fs.result(), fd.result()
        rec = store.get_floor(item["id"])
        assert rec["source"] == "seller" and rec["floor"] == 60.0


# --- items: size_bucket -----------------------------------------------------------------------


def test_item_size_bucket_is_writable_and_returned(store: Store) -> None:
    item = _item(store)
    assert item["size_bucket"] is None
    updated = store.update_item(item["id"], {"size_bucket": "large"})
    assert updated["size_bucket"] == "large"


# --- floors: step/rounds knobs ----------------------------------------------------------------


def test_floor_carries_optional_counter_knobs(store: Store) -> None:
    item = _item(store, list_price=80.0)
    ack = store.set_floor(item["id"], 60.0, "seller", auto_counter_step=15, auto_counter_rounds=3)
    assert "floor" not in ack
    rec = store.get_floor(item["id"])
    assert rec["auto_counter_step"] == 15 and rec["auto_counter_rounds"] == 3


# --- threads ----------------------------------------------------------------------------------


def test_create_sell_thread_requires_item_and_prefix(store: Store) -> None:
    item = _item(store)
    with pytest.raises(StoreError, match="item_id"):
        store.create_thread(thread_id="fb:1", side="sell", market="fb", counterpart_handle="bob")
    with pytest.raises(StoreError, match="start with"):
        store.create_thread(
            thread_id="wrong:1",
            side="sell",
            market="fb",
            counterpart_handle="bob",
            item_id=item["id"],
        )
    t = store.create_thread(
        thread_id="fb:1",
        side="sell",
        market="fb",
        counterpart_handle="bob",
        item_id=item["id"],
    )
    assert t["thread_id"] == "fb:1" and t["status"] == "active" and t["messages"] == []


def test_create_buy_thread_requires_want(store: Store) -> None:
    want = _want(store)
    with pytest.raises(StoreError, match="want_id"):
        store.create_thread(thread_id="cl:9", side="buy", market="cl", counterpart_handle="sue")
    t = store.create_thread(
        thread_id="cl:9",
        side="buy",
        market="cl",
        counterpart_handle="sue",
        want_id=want["want_id"],
    )
    assert t["side"] == "buy" and t["want_id"] == want["want_id"]


def test_create_thread_refuses_dangling_owner_and_duplicate(store: Store) -> None:
    with pytest.raises(ItemNotFound):
        store.create_thread(
            thread_id="fb:1",
            side="sell",
            market="fb",
            counterpart_handle="bob",
            item_id="item_nope",
        )
    item = _item(store)
    store.create_thread(
        thread_id="fb:1", side="sell", market="fb", counterpart_handle="bob", item_id=item["id"]
    )
    with pytest.raises(StoreError, match="already exists"):
        store.create_thread(
            thread_id="fb:1",
            side="sell",
            market="fb",
            counterpart_handle="bob",
            item_id=item["id"],
        )


def test_append_thread_message_dedup_by_constraint(store: Store) -> None:
    item = _item(store)
    store.create_thread(
        thread_id="fb:1", side="sell", market="fb", counterpart_handle="bob", item_id=item["id"]
    )
    assert store.append_thread_message("fb:1", msg_id="m1", direction="in", text="hi", ts=1.0)
    # same msg_id folds to a no-op (dedup by the UNIQUE constraint, not a read-then-write race)
    assert not store.append_thread_message("fb:1", msg_id="m1", direction="in", text="hi", ts=1.0)
    store.append_thread_message("fb:1", msg_id="m2", direction="out", text="yo", ts=2.0)
    thread = store.get_thread("fb:1")
    assert [m["msg_id"] for m in thread["messages"]] == ["m1", "m2"]
    assert thread["message_count"] == 2


def test_append_thread_message_missing_thread_raises(store: Store) -> None:
    with pytest.raises(ThreadNotFound):
        store.append_thread_message("fb:none", msg_id="m1", direction="in", text="hi")


def test_get_thread_caps_transcript_to_most_recent(store: Store) -> None:
    item = _item(store)
    store.create_thread(
        thread_id="fb:1", side="sell", market="fb", counterpart_handle="bob", item_id=item["id"]
    )
    for i in range(5):
        store.append_thread_message(
            "fb:1", msg_id=f"m{i}", direction="in", text=str(i), ts=float(i)
        )
    thread = store.get_thread("fb:1", message_cap=2)
    assert [m["msg_id"] for m in thread["messages"]] == ["m3", "m4"]  # most recent, chronological
    assert thread["message_count"] == 5


def test_list_threads_filters(store: Store) -> None:
    item = _item(store)
    want = _want(store)
    store.create_thread(
        thread_id="fb:1", side="sell", market="fb", counterpart_handle="b", item_id=item["id"]
    )
    store.create_thread(
        thread_id="cl:2", side="buy", market="cl", counterpart_handle="s", want_id=want["want_id"]
    )
    assert {t["thread_id"] for t in store.list_threads(side="sell")} == {"fb:1"}
    assert len(store.list_threads()) == 2


# --- wants ------------------------------------------------------------------------------------


def test_create_and_get_want_never_returns_budget(store: Store) -> None:
    want = _want(store, target_price=500.0, region="SG")
    assert want["want_id"].startswith("want_")
    assert want["status"] == "searching"
    fetched = store.get_want(want["want_id"])
    assert "max_budget" not in fetched and "budget" not in fetched
    assert fetched["target_price"] == 500.0


def test_create_want_rejects_blank_query(store: Store) -> None:
    with pytest.raises(StoreError):
        store.create_want(query="   ")


def test_list_wants_filters_by_status(store: Store) -> None:
    _want(store, query="a")
    _want(store, query="b")
    assert len(store.list_wants()) == 2
    assert store.list_wants(status="bought") == []


# --- budgets: engine-only reader --------------------------------------------------------------


def test_get_budget_absent_returns_none(store: Store) -> None:
    want = _want(store)
    assert store.get_budget(want["want_id"]) is None


# --- passes -----------------------------------------------------------------------------------


def test_enqueue_and_claim_single_flight(store: Store) -> None:
    p1 = store.enqueue_pass("publish", {"item_id": "item_1"})
    p2 = store.enqueue_pass("publish", {"item_id": "item_2"})
    assert store.count_queued_passes() == 2

    claimed = store.claim_queued_pass()
    assert claimed.pass_id == p1  # oldest first
    assert claimed.payload == {"item_id": "item_1"}
    assert store.get_pass(p1)["status"] == "running"
    assert store.get_pass(p1)["started_ts"] is not None
    assert store.count_queued_passes() == 1

    store.claim_queued_pass()
    assert store.claim_queued_pass() is None  # queue drained
    assert store.get_pass(p2)["status"] == "running"


def test_claim_is_atomic_under_concurrency(store: Store) -> None:
    ids = {store.enqueue_pass("publish", {"n": i}) for i in range(20)}
    claimed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(store.claim_queued_pass) for _ in range(40)]
        for f in futures:
            c = f.result()
            if c is not None:
                claimed.append(c.pass_id)
    assert sorted(claimed) == sorted(ids)  # each pass claimed exactly once


def test_finish_pass_stamps_outcome(store: Store) -> None:
    pid = store.enqueue_pass("publish", {})
    store.claim_queued_pass()
    store.finish_pass(pid, status="done", rc=0, cls="ok", summary="listed")
    row = store.get_pass(pid)
    assert row["status"] == "done" and row["rc"] == 0 and row["class"] == "ok"
    assert row["finished_ts"] is not None


def test_finish_pass_rejects_non_terminal_status(store: Store) -> None:
    pid = store.enqueue_pass("publish", {})
    with pytest.raises(StoreError):
        store.finish_pass(pid, status="running")


def test_fail_stale_running_fails_loud_never_reruns(store: Store) -> None:
    pid = store.enqueue_pass("publish", {})
    store.claim_queued_pass()  # -> running, started_ts = now
    # nothing stale yet
    assert store.fail_stale_running(max_age_sec=900) == []
    # far-future 'now' makes the running row look ancient
    ancient = store.get_pass(pid)["started_ts"] + 10000
    failed = store.fail_stale_running(max_age_sec=900, now=ancient)
    assert failed == [pid]
    row = store.get_pass(pid)
    assert row["status"] == "error" and row["class"] == "stale"
    assert store.count_queued_passes() == 0  # not re-queued
