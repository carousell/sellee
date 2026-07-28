"""The Q&A bank tools, the take-down, and the `agreed` send carve-out — the rest of the loop."""

from __future__ import annotations

import pytest

from selly_agent.config import Config
from selly_agent.store import Scope, ScopedStore
from selly_agent.tools.registry import Session, ToolContext, ToolError, UnknownTool, dispatch

_FAST = Config(reply_delay_sec=(0, 0), interactive_reply_delay_sec=(0, 0))


class FakeRail:
    """A rail recording the calls made on it, so a take-down can be checked without a network."""

    def __init__(self, *, fail=False):
        self.fail = fail
        self.updates: list = []

    def update_listing(self, listing_id, *, status):
        if self.fail:
            from selly_agent.rail.client import RailToolError

            raise RailToolError("listing not found")
        self.updates.append((listing_id, status))
        return {"ok": True}


class FakeSink:
    def __init__(self):
        self.sends: list = []

    def send(self, thread, text, kind, intent_id):
        self.sends.append((thread["thread_id"], text, kind))


def _item(store, **kwargs):
    return store.create_item(
        title=kwargs.pop("title", "Teak lamp"),
        list_price=kwargs.pop("list_price", 80.0),
        currency="SGD",
    )


# --- searching and banking ----------------------------------------------------------------------


def test_a_global_answer_reaches_every_item(make_ctx, store) -> None:
    """The global rows arrive through the search itself, not through the pass's scope — that is what
    lets a scoped reply pass see them at all."""
    item = _item(store)
    ctx = make_ctx("attended")
    dispatch(
        "add_qa_entry",
        {
            "item_id": "*",
            "question": "How do you pack fragile things?",
            "answer": "Double-boxed with bubble wrap.",
            "source": "seller",
        },
        ctx,
    )
    found = dispatch("search_qa_bank", {"item_id": item["id"]}, ctx)
    assert [entry["item_id"] for entry in found["entries"]] == ["*"]


def test_a_scoped_reply_pass_sees_its_items_answers_and_the_global_ones(store, bus) -> None:
    mine = _item(store)
    other = _item(store, title="Office chair")
    for target, question in ((mine["id"], "mine"), (other["id"], "theirs"), ("*", "global")):
        store.qa_add(target, question, "an answer", "seller")

    scope = Scope.of(threads={"carousell:1"}, items={mine["id"]})
    ctx = ToolContext(
        session=Session(tier="pass:reply", pass_id="p1", scope=scope),
        store=ScopedStore(store, scope),
        bus=bus,
        config=Config(),
    )
    found = dispatch("search_qa_bank", {"item_id": mine["id"]}, ctx)
    assert {entry["question"] for entry in found["entries"]} == {"mine", "global"}
    # another item's taught answers are simply not there
    assert dispatch("search_qa_bank", {"item_id": other["id"]}, ctx)["entries"] == []


def test_a_reply_pass_cannot_bank_an_answer(store, bus) -> None:
    """Banking records what the seller said. A pass that has only heard the buyer has nothing to
    record, and being able to would let a buyer write the answers future buyers get."""
    ctx = ToolContext(
        session=Session(tier="pass:reply", pass_id="p1", scope=Scope.of()),
        store=ScopedStore(store, Scope.of()),
        bus=bus,
        config=Config(),
    )
    with pytest.raises(UnknownTool):
        dispatch(
            "add_qa_entry",
            {"item_id": "*", "question": "q", "answer": "a", "source": "seller"},
            ctx,
        )


def test_a_miss_is_an_empty_result_not_an_error(make_ctx, store) -> None:
    item = _item(store)
    ctx = make_ctx("attended")
    found = dispatch("search_qa_bank", {"item_id": item["id"], "query": "anything"}, ctx)
    assert found == {"entries": [], "count": 0}


# --- the take-down ------------------------------------------------------------------------------


def _published(store, *, rail=True, browser=False):
    item = _item(store)
    if rail:
        store.record_listing_url(item["id"], "carousell-ai", "https://www.carousell.ai/listing/abc")
    if browser:
        store.record_listing_url(item["id"], "carousell", "https://www.carousell.sg/p/lamp-123/")
    return store.get_item(item["id"])


def test_a_take_down_archives_the_rail_listing_and_drops_its_url(make_ctx, store) -> None:
    item = _published(store)
    rail = FakeRail()
    ctx = make_ctx("attended", rail_factory=lambda: rail)
    res = dispatch(
        "carousell_ai_update_listing", {"item_id": item["id"], "action": "take_down"}, ctx
    )
    assert res["status"] == "taken_down"
    # archived, not sold: it did sell, but not here — saying otherwise would report a sale the rail
    # never had
    assert rail.updates == [("abc", "archived")]
    assert "carousell-ai" not in store.get_item(item["id"])["listing_urls"]


def test_the_url_is_only_dropped_after_the_rail_accepts(make_ctx, store) -> None:
    """A local record saying the listing is gone while it is still live would leave a buyer able to
    reach it with nothing watching the thread."""
    item = _published(store)
    ctx = make_ctx("attended", rail_factory=lambda: FakeRail(fail=True))
    with pytest.raises(ToolError, match="listing not found"):
        dispatch("carousell_ai_update_listing", {"item_id": item["id"], "action": "take_down"}, ctx)
    assert "carousell-ai" in store.get_item(item["id"])["listing_urls"]


def test_an_unlisted_item_is_a_no_op_not_a_failure(make_ctx, store) -> None:
    item = _item(store)
    ctx = make_ctx("attended", rail_factory=lambda: FakeRail())
    res = dispatch(
        "carousell_ai_update_listing", {"item_id": item["id"], "action": "take_down"}, ctx
    )
    assert res["status"] == "not_listed"


def test_a_browser_listing_becomes_a_named_needs_me_item(make_ctx, store) -> None:
    """The agent does not drive a take-down recipe, so the work it will not do has to be visible
    rather than silently skipped."""
    item = _published(store, browser=True)
    ctx = make_ctx("attended", rail_factory=lambda: FakeRail())
    res = dispatch(
        "carousell_ai_update_listing", {"item_id": item["id"], "action": "take_down"}, ctx
    )
    assert res["manual_take_downs"] == [
        {"market": "carousell", "url": "https://www.carousell.sg/p/lamp-123/"}
    ]
    notices = store.list_queued_notices()
    assert len(notices) == 1
    assert "carousell.sg/p/lamp-123" in notices[0]["text"]
    assert notices[0]["ref"] == item["id"]


def test_the_sold_flow_reaches_the_take_down(make_ctx, store) -> None:
    """negotiate_confirm_sold names the listings to close; this is the tool that closes the rail
    one, which is what makes the sold flow finishable at all."""
    item = _published(store, browser=True)
    store.create_thread(
        thread_id="carousell:1",
        side="sell",
        market="carousell",
        counterpart_handle="bob",
        item_id=item["id"],
    )
    sold = store.negotiate_confirm_sold(item["id"], "carousell:1")
    assert {row["platform"] for row in sold["take_down"]} == {"carousell-ai"}

    ctx = make_ctx("attended", rail_factory=lambda: FakeRail())
    res = dispatch(
        "carousell_ai_update_listing", {"item_id": item["id"], "action": "take_down"}, ctx
    )
    assert res["status"] == "taken_down"


def test_a_reply_pass_cannot_take_a_listing_down(store, bus) -> None:
    ctx = ToolContext(
        session=Session(tier="pass:reply", pass_id="p1", scope=Scope.of()),
        store=ScopedStore(store, Scope.of()),
        bus=bus,
        config=Config(),
    )
    with pytest.raises(UnknownTool):
        dispatch("carousell_ai_update_listing", {"item_id": "item_1", "action": "take_down"}, ctx)


# --- the `agreed` carve-out ---------------------------------------------------------------------


def _agreed_thread(store):
    item = _item(store)
    store.create_thread(
        thread_id="carousell:1",
        side="sell",
        market="carousell",
        counterpart_handle="bob",
        item_id=item["id"],
    )
    with store._db.transaction() as conn:  # noqa: SLF001 — the sale flow owns this status
        conn.execute("UPDATE threads SET status='agreed' WHERE thread_id='carousell:1'")
    return item


def test_an_agreed_thread_takes_a_reply_so_the_checkout_link_can_reach_the_buyer(
    make_ctx, store
) -> None:
    _agreed_thread(store)
    sink = FakeSink()
    ctx = make_ctx("attended", reply_sink=sink, config=_FAST)
    res = dispatch(
        "send_reply",
        {"thread_id": "carousell:1", "text": "here's your checkout link: …", "kind": "reply"},
        ctx,
    )
    assert res["status"] == "sent"
    assert len(sink.sends) == 1


@pytest.mark.parametrize("kind", ["followup", "nudge", "holding"])
def test_an_agreed_thread_refuses_everything_that_is_not_a_reply(make_ctx, store, kind) -> None:
    """The price is settled; a nudge or a fresh negotiation on a done deal is not a reply."""
    _agreed_thread(store)
    ctx = make_ctx("attended", reply_sink=FakeSink(), config=_FAST)
    with pytest.raises(ToolError, match="not eligible"):
        dispatch(
            "send_reply", {"thread_id": "carousell:1", "text": "still there?", "kind": kind}, ctx
        )


def test_the_other_terminal_statuses_still_refuse_a_reply(make_ctx, store) -> None:
    _agreed_thread(store)
    store.hold_thread("carousell:1", reason="scam")
    ctx = make_ctx("attended", reply_sink=FakeSink(), config=_FAST)
    with pytest.raises(ToolError, match="not eligible"):
        dispatch("send_reply", {"thread_id": "carousell:1", "text": "hi", "kind": "reply"}, ctx)
