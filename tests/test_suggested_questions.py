"""The suggested questions the listing confirmation offers: the common buyer questions for an
item's category, minus any its Q&A bank already answers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import sellee.tools  # noqa: F401  registration
from sellee.paths import PACKAGE_DATA_DIR
from sellee.store import Scope
from sellee.tools import suggested_questions
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    ToolError,
    UnknownTool,
    dispatch,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "suggested_questions.json"
_ELECTRONICS = json.loads(_FIXTURE.read_text())["small_electronics"]


@pytest.fixture(autouse=True)
def _fixture_lists(monkeypatch):
    monkeypatch.setattr(suggested_questions, "QUESTIONS_PATH", _FIXTURE)
    suggested_questions.questions_by_category.cache_clear()
    yield
    suggested_questions.questions_by_category.cache_clear()


@pytest.fixture
def ctx(make_ctx):
    return make_ctx(TIER_ATTENDED)


def _item(store, category="small_electronics"):
    return store.create_item(title="Phone", list_price=200.0, currency="SGD", category=category)


def _suggest(ctx, item_id):
    return dispatch("suggested_questions", {"item_id": item_id}, ctx)["questions"]


def test_the_first_three_in_the_files_order(ctx, store) -> None:
    assert _suggest(ctx, _item(store)["id"]) == _ELECTRONICS[:3]


def test_a_banked_question_is_left_out_and_the_next_one_fills_in(ctx, store) -> None:
    item = _item(store)
    store.qa_add(item["id"], "What's the battery health?", "91%", "seller")
    assert _suggest(ctx, item["id"]) == _ELECTRONICS[1:4]


def test_a_global_answer_to_the_same_question_counts_as_answered(ctx, store) -> None:
    # search_qa_bank hands a buyer's question the global rows too, so it is answered already.
    item = _item(store)
    store.qa_add("*", "Does it come with the charger?", "Always, with everything I sell.", "seller")
    assert "Does it come with the charger?" not in _suggest(ctx, item["id"])


def test_another_items_answer_does_not_count(ctx, store) -> None:
    other = _item(store)
    store.qa_add(other["id"], "What's the battery health?", "80%", "seller")
    assert _suggest(ctx, _item(store)["id"]) == _ELECTRONICS[:3]


def test_a_question_answered_beyond_the_search_cap_is_still_left_out(ctx, store) -> None:
    # search_qa_bank returns 20 rows; an older answer behind newer ones still answers the question.
    item = _item(store)
    store.qa_add(item["id"], "What's the battery health?", "91%", "seller")
    for n in range(25):
        store.qa_add(item["id"], f"Question {n}?", "Answer.", "seller")
    assert "What's the battery health?" not in _suggest(ctx, item["id"])


def test_a_short_list_returns_what_it_has(ctx, store) -> None:
    assert _suggest(ctx, _item(store, "fashion")["id"]) == ["What size is it?"]


def test_every_question_answered_returns_none(ctx, store) -> None:
    item = _item(store, "fashion")
    store.qa_add(item["id"], "What size is it?", "M", "seller")
    assert _suggest(ctx, item["id"]) == []


@pytest.mark.parametrize("category", [None, "", "weapons", "not_a_token"])
def test_no_list_for_the_category_returns_none(ctx, store, category) -> None:
    assert _suggest(ctx, _item(store, category)["id"]) == []


def test_an_unknown_item_is_an_error(ctx) -> None:
    with pytest.raises(ToolError, match="no item"):
        _suggest(ctx, "nope")


def test_an_item_outside_a_scoped_pass_reads_as_unknown(make_ctx, store) -> None:
    mine, other = _item(store), _item(store)
    scoped = make_ctx(TIER_PASS_CHANNEL, pass_id="p1", scope=Scope.of(items={mine["id"]}))
    assert _suggest(scoped, mine["id"]) == _ELECTRONICS[:3]
    with pytest.raises(ToolError, match="no item"):
        _suggest(scoped, other["id"])


def test_the_listing_conversation_can_call_it_and_a_buyer_reply_cannot(make_ctx, store) -> None:
    item = _item(store)
    for tier in (TIER_ATTENDED, TIER_PASS_CHANNEL):
        assert _suggest(make_ctx(tier), item["id"]) == _ELECTRONICS[:3]
    with pytest.raises(UnknownTool):
        _suggest(make_ctx("pass:reply", pass_id="r1"), item["id"])


_BANKABLE = st.sampled_from(_ELECTRONICS + ["Is it waterproof?", "what's the battery health?"])


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(banked=st.lists(_BANKABLE, max_size=6))
def test_never_more_than_three_and_never_one_already_answered(ctx, store, banked) -> None:
    item = _item(store)
    for question in banked:
        store.qa_add(item["id"], question, "an answer", "seller")
    answered = set(banked)
    questions = _suggest(ctx, item["id"])
    assert len(questions) <= 3
    # Matched on the exact question text, as qa_add stored it.
    assert not answered & set(questions)
    assert questions == [q for q in _ELECTRONICS if q not in answered][:3]


def test_the_shipped_lists_load_and_hold_only_questions() -> None:
    suggested_questions.questions_by_category.cache_clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            suggested_questions, "QUESTIONS_PATH", PACKAGE_DATA_DIR / "suggested_questions.json"
        )
        lists = suggested_questions.questions_by_category()
    assert lists
    for token, questions in lists.items():
        assert token == token.strip().lower() and " " not in token, token
        assert questions, token
        assert len(set(questions)) == len(questions), token
        assert all(q == q.strip() and q.endswith("?") for q in questions), token
