"""Store accessors the fan-out adds: the publish-attempt index, the unreported-outcome queue and
its report-once transaction, the settled-sale read, and the cross-link push marker."""

from __future__ import annotations


def _publish(store, item_id, market, *, origin="crosslist"):
    return store.enqueue_pass("publish", {"item_id": item_id, "market": market, "origin": origin})


def _settle(store, pass_id, *, status="done", cls="ok"):
    store.finish_pass(pass_id, status=status, rc=0 if status == "done" else 1, cls=cls, summary=cls)


# --- the attempt index ------------------------------------------------------------------------


def test_publish_pass_index_reports_every_attempt_with_its_status(store) -> None:
    item = store.create_item(title="Lamp", list_price=80.0)
    queued = _publish(store, item["id"], "carousell")
    failed = _publish(store, item["id"], "fb")
    _settle(store, failed, status="error", cls="error")

    index = {(row["market"], row["status"]) for row in store.publish_pass_index()}
    assert index == {("carousell", "queued"), ("fb", "error")}
    assert {row["item_id"] for row in store.publish_pass_index()} == {item["id"]}
    assert queued  # the queued row is in the index too — an attempt in flight is still an attempt


def test_publish_pass_index_carries_a_rail_publish_with_no_market(store) -> None:
    """A payload with no market predates the browser publish path and means the rail. The store
    reports what is there and lets the caller decide what it means."""
    item = store.create_item(title="Lamp", list_price=80.0)
    store.enqueue_pass("publish", {"item_id": item["id"]})
    assert store.publish_pass_index() == [
        {
            "market": None,
            "item_id": item["id"],
            "origin": None,
            "status": "queued",
            # None until it settles — what the retry clock reads, and never set on a queued row.
            "finished_ts": None,
        }
    ]


def test_publish_pass_index_ignores_other_pass_types(store) -> None:
    store.enqueue_pass("channel", {})
    store.enqueue_pass("reply", {"thread_ids": ["carousell:1"]})
    assert store.publish_pass_index() == []


# --- the outcome queue ------------------------------------------------------------------------


def test_unreported_covers_only_settled_crosslist_passes(store) -> None:
    item = store.create_item(title="Lamp", list_price=80.0)
    running = _publish(store, item["id"], "carousell")
    attended = _publish(store, item["id"], "fb", origin=None)
    _settle(store, attended)
    done = _publish(store, item["id"], "mercari")
    _settle(store, done)

    pending = store.unreported_crosslist_passes()
    assert [row["pass_id"] for row in pending] == [done]
    assert pending[0]["market"] == "mercari"
    assert pending[0]["status"] == "done"
    assert running  # still running, so there is no outcome to report yet


def test_report_is_one_transaction_and_happens_once(store) -> None:
    item = store.create_item(title="Lamp", list_price=80.0)
    pass_id = _publish(store, item["id"], "carousell")
    _settle(store, pass_id)

    assert store.report_crosslist_pass(pass_id, "Listed on Carousell: https://x", ref=item["id"])
    assert store.unreported_crosslist_passes() == []
    notices = store.claim_queued_notices(10)
    assert [n["text"] for n in notices] == ["Listed on Carousell: https://x"]
    assert notices[0]["ref"] == item["id"]

    # A second sweep — or a retry after a crash — must not announce the same listing again.
    assert not store.report_crosslist_pass(pass_id, "Listed on Carousell: https://x")


def test_a_pass_that_owes_no_report_is_closed_out_once_seen(store) -> None:
    """A hand-run publish is never reported, so the first sweep that sees its settled row closes
    the flag — the scan stays bounded by work owed, not by a growing tail of CLI history."""
    item = store.create_item(title="Lamp", list_price=80.0)
    attended = _publish(store, item["id"], "carousell", origin=None)
    _settle(store, attended)

    assert store.unreported_crosslist_passes() == []
    rows = store._db.query("SELECT reported FROM passes WHERE pass_id = ?", (attended,))
    assert rows[0]["reported"] == 1
    # And closing it queued nothing — the flag means "no report owed", not "reported".
    assert store.claim_queued_notices(10) == []


def test_an_already_reported_pass_stays_out_of_the_queue(store) -> None:
    """What the migration's `UPDATE passes SET reported = 1` leaves behind: rows that settled before
    the fan-out existed are never replayed on upgrade."""
    item = store.create_item(title="Lamp", list_price=80.0)
    pass_id = _publish(store, item["id"], "carousell")
    _settle(store, pass_id)
    with store._db.transaction() as conn:
        conn.execute("UPDATE passes SET reported = 1 WHERE pass_id = ?", (pass_id,))
    assert store.unreported_crosslist_passes() == []


# --- settled sales ----------------------------------------------------------------------------


def test_sold_item_ids_reads_the_negotiation_ledger(store) -> None:
    sold = store.create_item(title="Lamp", list_price=80.0)
    open_item = store.create_item(title="Chair", list_price=20.0)
    store.create_thread(
        thread_id="carousell:1",
        side="sell",
        market="carousell",
        counterpart_handle="buyer",
        item_id=sold["id"],
    )
    store.negotiate_confirm_sold(sold["id"], "carousell:1")

    ids = store.sold_item_ids()
    assert sold["id"] in ids
    assert open_item["id"] not in ids


# --- the cross-link push marker ----------------------------------------------------------------


def test_crosslink_marker_upserts_and_reads_back(store) -> None:
    item = store.create_item(title="Lamp", list_price=80.0)
    assert store.crosslink_pushed_urls() == {}

    store.set_crosslink_pushed(item["id"], '[{"platform": "P", "url": "https://a"}]')
    assert store.crosslink_pushed_urls() == {item["id"]: '[{"platform": "P", "url": "https://a"}]'}

    store.set_crosslink_pushed(item["id"], "[]")
    assert store.crosslink_pushed_urls() == {item["id"]: "[]"}


def test_crosslink_marker_rows_follow_their_item(store) -> None:
    """ON DELETE CASCADE: a deleted item takes its marker with it, so the table never grows a
    tail of orphans."""
    item = store.create_item(title="Lamp", list_price=80.0)
    store.set_crosslink_pushed(item["id"], "[]")

    with store._db.transaction() as conn:
        conn.execute("DELETE FROM items WHERE id = ?", (item["id"],))
    assert store.crosslink_pushed_urls() == {}
