"""Escalations: escalate flips the thread + emits an event and is idempotent; resolve stamps +
emits; get_status counts open ones; follow-up eligibility excludes open-escalation threads; a
synthetic (non-existent) thread id is refused."""

from __future__ import annotations

import pytest

from sellee.tools.registry import ToolError, dispatch


def _sell_thread(store, tid="fb:1"):
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    store.create_thread(
        thread_id=tid, side="sell", market="fb", counterpart_handle="bob", item_id=item["id"]
    )
    return item


def test_escalate_flips_thread_and_emits_event(make_ctx, store, bus) -> None:
    _sell_thread(store)
    ctx = make_ctx("attended", pass_id="p1")
    res = dispatch(
        "escalate",
        {"thread_id": "fb:1", "open_question": "buyer wants to meet — ok?", "kind": "handover"},
        ctx,
    )
    assert res["idempotent"] is False
    assert store.get_thread("fb:1")["status"] == "escalated"
    events = bus.store.read(kinds=["escalation.open"])
    assert len(events) == 1 and events[0].payload["thread_id"] == "fb:1"


def test_escalate_is_idempotent(make_ctx, store, bus) -> None:
    _sell_thread(store)
    ctx = make_ctx("attended")
    first = dispatch("escalate", {"thread_id": "fb:1", "open_question": "q"}, ctx)
    second = dispatch("escalate", {"thread_id": "fb:1", "open_question": "q again"}, ctx)
    assert second["idempotent"] is True and second["id"] == first["id"]
    # only the first opened an event
    assert len(bus.store.read(kinds=["escalation.open"])) == 1


def test_escalate_persists_its_options(make_ctx, store) -> None:
    _sell_thread(store)
    ctx = make_ctx("attended")
    opened = dispatch(
        "escalate",
        {
            "thread_id": "fb:1",
            "open_question": "meet at Orchard, or checkout?",
            "options": ["🔗 Send checkout link", "🤝 I'll handle it"],
        },
        ctx,
    )
    assert store.get_escalation(opened["id"])["options"] == [
        "🔗 Send checkout link",
        "🤝 I'll handle it",
    ]


def test_escalate_without_options_reads_back_as_none(make_ctx, store) -> None:
    _sell_thread(store)
    opened = dispatch(
        "escalate",
        {"thread_id": "fb:1", "open_question": "how should I answer?"},
        make_ctx("attended"),
    )
    assert store.get_escalation(opened["id"])["options"] is None


def test_a_re_escalate_never_swaps_the_buttons_underneath(make_ctx, store) -> None:
    """The seller is looking at a message whose buttons were already sent — changing the row's
    options would leave those buttons offering answers the row no longer holds."""
    _sell_thread(store)
    ctx = make_ctx("attended")
    first = dispatch(
        "escalate", {"thread_id": "fb:1", "open_question": "q", "options": ["A", "B"]}, ctx
    )
    second = dispatch(
        "escalate", {"thread_id": "fb:1", "open_question": "q", "options": ["C", "D"]}, ctx
    )
    assert second["id"] == first["id"] and second["idempotent"] is True
    assert store.get_escalation(first["id"])["options"] == ["A", "B"]


@pytest.mark.parametrize(
    "options",
    [["only one"], ["a", "b", "c", "d", "e"], ["Accept", "x" * 65], ["Accept", "Accept"]],
)
def test_escalate_rejects_malformed_options(make_ctx, store, options) -> None:
    _sell_thread(store)
    with pytest.raises(ToolError):
        dispatch(
            "escalate",
            {"thread_id": "fb:1", "open_question": "q", "options": options},
            make_ctx("attended"),
        )
    # Nothing was opened — the ask is rejected whole, never half-recorded.
    assert store.count_open_escalations() == 0


def test_escalate_requires_a_real_thread(make_ctx, store) -> None:
    ctx = make_ctx("attended")
    with pytest.raises(ToolError, match="no thread"):
        dispatch("escalate", {"thread_id": "fb:synthetic", "open_question": "q"}, ctx)


def test_resolve_stamps_and_emits(make_ctx, store, bus) -> None:
    _sell_thread(store)
    ctx = make_ctx("attended")
    opened = dispatch("escalate", {"thread_id": "fb:1", "open_question": "q"}, ctx)
    res = dispatch(
        "resolve_escalation", {"escalation_id": opened["id"], "resolution": "confirmed ok"}, ctx
    )
    assert res["status"] == "resolved"
    assert len(bus.store.read(kinds=["escalation.resolved"])) == 1
    assert store.count_open_escalations() == 0


def test_get_status_counts_open_escalations(make_ctx, store) -> None:
    _sell_thread(store)
    ctx = make_ctx("attended")
    assert dispatch("get_status", {}, ctx)["open_escalations"] == 0
    dispatch("escalate", {"thread_id": "fb:1", "open_question": "q"}, ctx)
    assert dispatch("get_status", {}, ctx)["open_escalations"] == 1


def test_followup_eligibility_excludes_open_escalations(make_ctx, store) -> None:
    _sell_thread(store, "fb:1")
    _sell_thread(store, "fb:2")
    ctx = make_ctx("attended")
    dispatch("escalate", {"thread_id": "fb:1", "open_question": "q"}, ctx)
    excluded = store.open_escalation_thread_ids()
    assert excluded == {"fb:1"}
    # the query a follow-up lane would run: active threads not in the excluded set
    eligible = [
        t["thread_id"]
        for t in store.list_threads(side="sell")
        if t["status"] == "active" and t["thread_id"] not in excluded
    ]
    assert eligible == ["fb:2"]
