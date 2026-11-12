"""The published-description composer: the item's own description plus its listing-time answers."""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sellee.listing_description import HEADING, compose, published_description

_PROPERTY = settings(
    max_examples=80, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
_QUESTIONS = ("Does it come with the charger?", "Any marks or scratches?", "Is it smoke-free?")
_TEXT = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=40)
_ANSWER = _TEXT.filter(lambda s: s.strip())
# One banked row: (question, answer, marked for the listing).
_ROW = st.tuples(st.sampled_from(_QUESTIONS), _ANSWER, st.booleans())
# Descriptions that try to look like a section, or part of one.
_LINE = st.one_of(
    _TEXT,
    st.sampled_from(["", HEADING, "Q: Any marks?", "A: None", "Q:", "A:", f"{HEADING}:"]),
)
_DESCRIPTION = st.one_of(st.none(), _TEXT, st.lists(_LINE, max_size=8).map("\n".join))
_PLAIN = st.one_of(st.none(), _TEXT.filter(lambda s: HEADING not in s))


def _item(store, description):
    return store.create_item(title="Phone", list_price=80.0, description=description)


def _bank(store, item_id, rows):
    for question, answer, marked in rows:
        store.qa_add(item_id, question, answer, "seller", on_listing=marked)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _latest_marked(rows) -> dict:
    latest: dict = {}
    for question, answer, marked in rows:
        if marked:
            latest.pop(question, None)
            latest[question] = answer
    return latest


@_PROPERTY
@given(description=_PLAIN, rows=st.lists(_ROW, max_size=8))
def test_every_marked_question_appears_once_with_its_latest_answer(store, description, rows):
    item = _item(store, description)
    _bank(store, item["id"], rows)
    out = published_description(store, store.get_item(item["id"]))
    latest = _latest_marked(rows)
    if not latest:
        assert out == (description or "")
        return
    own, _, section = out.rpartition(HEADING)
    assert own.rstrip() == (description or "").rstrip()
    lines = section.strip("\n").split("\n")
    pairs = dict(zip(lines[0::2], lines[1::2], strict=True))
    assert len(pairs) == len(lines) // 2  # no question twice
    assert pairs == {f"Q: {q}": f"A: {_flat(a)}" for q, a in latest.items()}


@_PROPERTY
@given(description=_DESCRIPTION, rows=st.lists(_ROW, max_size=8))
def test_composing_twice_adds_nothing(store, description, rows):
    item = _item(store, description)
    _bank(store, item["id"], rows)
    once = published_description(store, store.get_item(item["id"]))
    answers = store.qa_listing_answers(item["id"])
    assert compose(once, answers) == once
    # A listing read back from a marketplace carries the section; republishing it keeps one.
    adopted = _item(store, once)
    _bank(store, adopted["id"], rows)
    assert published_description(store, store.get_item(adopted["id"])) == once


@_PROPERTY
@given(description=_DESCRIPTION, rows=st.lists(_ROW.map(lambda r: (*r[:2], False)), max_size=6))
def test_no_marked_answers_leaves_the_description_as_the_seller_wrote_it(store, description, rows):
    item = _item(store, description)
    _bank(store, item["id"], rows)  # escalation answers only
    assert published_description(store, store.get_item(item["id"])) == (description or "")


@_PROPERTY
@given(rows=st.lists(_ROW, min_size=1, max_size=8))
def test_an_escalation_answer_never_appears(store, rows):
    item = _item(store, "Barely used.")
    _bank(store, item["id"], rows)
    store.qa_add(item["id"], "Can you deliver to Jurong?", "Only on weekends.", "seller")
    out = published_description(store, store.get_item(item["id"]))
    assert "Jurong" not in out and "weekends" not in out


def test_the_section_reads_as_a_plain_heading_and_question_answer_pairs(store) -> None:
    item = _item(store, "iPhone 13, 128GB.\n")
    for question, answer in (
        ("Does it come with the charger?", "Yes"),
        ("Any marks?", "One small\nscratch  on the back"),
        ("Does it come with the charger?", "Cable only"),
    ):
        store.qa_add(item["id"], question, answer, "seller", on_listing=True)

    assert published_description(store, store.get_item(item["id"])) == (
        "iPhone 13, 128GB.\n"
        "\n"
        "Common questions\n"
        "Q: Does it come with the charger?\n"
        "A: Cable only\n"
        "Q: Any marks?\n"
        "A: One small scratch on the back"
    )


def test_an_item_with_no_description_gets_the_section_alone(store) -> None:
    item = _item(store, None)
    store.qa_add(item["id"], "Any marks?", "None", "seller", on_listing=True)
    assert published_description(store, store.get_item(item["id"])) == (
        "Common questions\nQ: Any marks?\nA: None"
    )


def test_another_items_answers_stay_on_that_item(store) -> None:
    mine, other = _item(store, "Mine."), _item(store, "Other.")
    store.qa_add(other["id"], "Any marks?", "A dent", "seller", on_listing=True)
    assert published_description(store, store.get_item(mine["id"])) == "Mine."
