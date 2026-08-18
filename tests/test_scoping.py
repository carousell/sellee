"""Entity scoping enforced at the data accessor: a scoped session sees only the threads/items/
wants it was spawned with, and an out-of-scope id is indistinguishable from a missing row —
scope must never leak existence. Attended sessions (scope None) see everything."""

from __future__ import annotations

import pytest

from sellee.store import ItemNotFound, Scope, ScopedStore, ThreadNotFound
from sellee.tools.registry import Session, ToolContext, ToolError, dispatch


def _two_of_everything(store):
    i1 = store.create_item(title="A", list_price=80.0, currency="SGD")
    i2 = store.create_item(title="B", list_price=90.0, currency="SGD")
    store.create_thread(
        thread_id="fb:1", side="sell", market="fb", counterpart_handle="b", item_id=i1["id"]
    )
    store.create_thread(
        thread_id="fb:2", side="sell", market="fb", counterpart_handle="c", item_id=i2["id"]
    )
    w1 = store.create_want(query="one")
    w2 = store.create_want(query="two")
    return i1, i2, w1, w2


def test_out_of_scope_reads_answer_like_missing(store) -> None:
    i1, i2, w1, w2 = _two_of_everything(store)
    scope = Scope.of(threads={"fb:1"}, items={i1["id"]}, wants={w1["want_id"]})
    scoped = ScopedStore(store, scope)

    # in scope: real rows
    assert scoped.get_item(i1["id"])["id"] == i1["id"]
    assert scoped.get_thread("fb:1")["thread_id"] == "fb:1"
    assert scoped.get_want(w1["want_id"])["want_id"] == w1["want_id"]

    # out of scope: exactly what a missing row returns (None), never a distinguishable error
    assert scoped.get_item(i2["id"]) is None
    assert scoped.get_thread("fb:2") is None
    assert scoped.get_want(w2["want_id"]) is None
    # a genuinely absent id behaves identically
    assert scoped.get_item("item_absent") is None
    assert scoped.get_thread("fb:absent") is None


def test_out_of_scope_transcript_read_is_empty_like_missing(store) -> None:
    i1, i2, _, _ = _two_of_everything(store)
    store.append_thread_message("fb:2", msg_id="m1", direction="in", text="secret", ts=1.0)
    scoped = ScopedStore(store, Scope.of(threads={"fb:1"}, items={i1["id"]}))
    assert scoped.get_thread_messages("fb:2") == []  # cannot even read another thread's transcript
    assert scoped.get_thread_messages("fb:1") == []  # in scope but empty — same shape


def test_out_of_scope_write_raises_like_missing(store) -> None:
    i1, _, _, _ = _two_of_everything(store)
    scoped = ScopedStore(store, Scope.of(threads={"fb:1"}, items={i1["id"]}))
    # appending to an out-of-scope thread raises the same NotFound a truly-missing thread does
    with pytest.raises(ThreadNotFound, match="fb:2"):
        scoped.append_thread_message("fb:2", msg_id="x", direction="in", text="hi")


def test_the_browser_layers_accessors_are_scope_guarded(store) -> None:
    """Every accessor the browser layer adds takes a scoped id, so each needs an entry in both the
    guard map and the miss-behavior map — a guard without a miss entry crashes at deny time."""
    i1, i2, _, _ = _two_of_everything(store)
    scoped = ScopedStore(store, Scope.of(threads={"fb:1"}, items={i1["id"]}))
    store.qa_add(i2["id"], "Colour?", "Teak.", "seller")

    # in scope: the real thing
    scoped.record_inbound("fb:1", msg_id="m1", text="hi", ts=1.0)
    assert store.get_thread("fb:1")["message_count"] == 1
    scoped.qa_add(i1["id"], "Chips?", "None.", "seller")
    assert [r["question"] for r in scoped.qa_search(i1["id"])] == ["Chips?"]

    # out of scope: exactly the missing-row behavior, so scope never leaks existence
    with pytest.raises(ThreadNotFound, match="fb:2"):
        scoped.record_inbound("fb:2", msg_id="m1", text="hi")
    with pytest.raises(ItemNotFound, match=i2["id"]):
        scoped.qa_add(i2["id"], "q", "a", "seller")
    assert scoped.qa_search(i2["id"]) == []  # another item's taught answers stay invisible


def test_list_reads_are_filtered_to_scope(store) -> None:
    i1, _, _, _ = _two_of_everything(store)
    scoped = ScopedStore(store, Scope.of(threads={"fb:1"}, items={i1["id"]}))
    assert {t["thread_id"] for t in scoped.list_threads()} == {"fb:1"}
    assert {i["id"] for i in scoped.list_items()} == {i1["id"]}


def test_unscoped_store_is_transparent(store) -> None:
    _two_of_everything(store)
    scoped = ScopedStore(store, None)  # attended sessions hold full scope
    assert scoped.get_thread("fb:2")["thread_id"] == "fb:2"
    assert len(scoped.list_threads()) == 2


def test_scope_enforced_through_dispatch(store, bus) -> None:
    i1, i2, _, _ = _two_of_everything(store)
    scope = Scope.of(threads={"fb:1"}, items={i1["id"]})
    session = Session(tier="attended", pass_id="pass_1", scope=scope)
    ctx = ToolContext(session=session, store=ScopedStore(store, scope), bus=bus, config=None)

    got = dispatch("get_item", {"item_id": i1["id"]}, ctx)
    assert got["id"] == i1["id"]

    # An out-of-scope item surfaces the same not-found ToolError a missing item would — scope
    # hides the row, not the tool (a ToolError, never an UnknownTool).
    with pytest.raises(ToolError, match=i2["id"]):
        dispatch("get_item", {"item_id": i2["id"]}, ctx)
