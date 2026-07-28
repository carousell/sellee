"""Reconcile: turning a thread tail into the rows that are not stored yet.

The properties that matter are idempotency (re-reading an unchanged tail inserts nothing), honesty
about our own and the seller's outbound messages (never recorded twice, never re-replied to), and
conservatism when a row cannot be attributed to exactly one listing.
"""

from __future__ import annotations

import pytest

from selly_agent.browser.markets.carousell import LISTING_ID_PATTERN as PATTERN
from selly_agent.browser.reconcile import (
    classify_tail,
    listing_id,
    match_item,
    message_id,
    new_rows,
    normalize,
    preview_matches,
)


def _bubble(text, side="in"):
    return {"text": text, "side": side, "y": 0}


def _recorded(rows):
    return [{"dir": d, "text": t} for d, t in rows]


# --- the tail read ------------------------------------------------------------------------------


def test_time_separators_and_centre_rows_are_dropped() -> None:
    """A centred row is a system banner or an offer widget. Keeping one would both record it as a
    message and let it stand as "someone answered", which would silence the thread."""
    rows = [
        _bubble("3:18 PM"),
        _bubble("Yesterday"),
        _bubble("12/07"),
        {"text": "Offer accepted", "side": "center", "y": 1},
        _bubble("still available?"),
    ]
    assert [row["text"] for row in classify_tail(rows)] == ["still available?"]


def test_the_tail_is_capped_to_its_trailing_bubbles() -> None:
    rows = [_bubble(f"m{i}") for i in range(20)]
    assert [row["text"] for row in classify_tail(rows, cap=3)] == ["m17", "m18", "m19"]


# --- normalization and ids ----------------------------------------------------------------------


def test_normalize_collapses_whitespace_case_and_truncation() -> None:
    assert normalize("  Still   AVAILABLE? ") == "still available?"
    assert normalize("a long message…") == normalize("a long message")


def test_the_same_message_always_gets_the_same_id() -> None:
    assert message_id("in", "Still available?", 1) == message_id("in", "still  available?", 1)
    assert message_id("in", "hi", 1) != message_id("in", "hi", 2)
    assert message_id("in", "hi", 1) != message_id("out", "hi", 1)


# --- idempotency --------------------------------------------------------------------------------


def test_re_reading_an_unchanged_tail_yields_nothing() -> None:
    tail = [_bubble("hi"), _bubble("you there?")]
    first = new_rows(tail, [], now=100.0)
    recorded = _recorded([(row["direction"], row["text"]) for row in first])
    assert new_rows(tail, recorded, now=200.0) == []


def test_only_the_genuinely_new_bubble_is_returned() -> None:
    tail = [_bubble("hi"), _bubble("you there?")]
    recorded = _recorded([("in", "hi")])
    assert [row["text"] for row in new_rows(tail, recorded, now=100.0)] == ["you there?"]


def test_timestamps_step_forward_so_stored_order_matches_the_screen() -> None:
    rows = new_rows([_bubble("one"), _bubble("two")], [], now=100.0)
    assert rows[0]["ts"] < rows[1]["ts"]


# --- repeated identical text ---------------------------------------------------------------------


def test_a_buyer_repeating_themselves_gets_a_second_row() -> None:
    recorded = _recorded([("in", "ok")])
    rows = new_rows([_bubble("ok"), _bubble("ok")], recorded, now=100.0)
    assert len(rows) == 1  # the first "ok" is already stored, the second is new


def test_a_message_scrolling_out_of_the_tail_is_not_re_inserted() -> None:
    """Counting copies against what is stored — not numbering positions in the window — is what
    makes this safe: as the conversation grows, old bubbles leave the tail and must not return."""
    recorded = _recorded([("in", "ok"), ("in", "ok")])
    assert new_rows([_bubble("ok"), _bubble("later")], recorded, now=100.0) == [
        {
            "msg_id": message_id("in", "later", 1),
            "direction": "in",
            "text": "later",
            "ts": 100.0,
        }
    ]


# --- our own and the seller's outbound messages --------------------------------------------------


def test_a_reply_we_sent_is_not_recorded_twice() -> None:
    """Our committed reply is stored under the send bracket's own id, so the matching bubble must
    reconcile against it by content — otherwise every reply would double-record."""
    recorded = _recorded([("in", "still available?"), ("out", "yes it is!")])
    tail = [_bubble("still available?"), _bubble("yes it is!", "out")]
    assert new_rows(tail, recorded, now=100.0) == []


def test_a_manual_seller_reply_is_recorded_once_as_outbound() -> None:
    """The seller answered in their own app. Recording it keeps the transcript truthful, and the
    outbound row is what stops the agent from talking over them."""
    recorded = _recorded([("in", "still available?")])
    tail = [_bubble("still available?"), _bubble("yep, posting today", "out")]
    rows = new_rows(tail, recorded, now=100.0)
    assert [(row["direction"], row["text"]) for row in rows] == [("out", "yep, posting today")]
    assert new_rows(tail, recorded + _recorded([("out", "yep, posting today")]), now=200.0) == []


# --- the skip gate ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("preview", "message", "expected"),
    [
        ("bob 3:18 PM Teak lamp still available?", "still available?", True),
        ("bob 3:18 PM Teak lamp still availa…", "still available?", True),
        ("bob 3:18 PM Teak lamp what's your best price?", "still available?", False),
        ("", "still available?", False),
        ("bob 3:18 PM Teak lamp anything", "", False),
    ],
)
def test_the_preview_gate_recognises_the_message_it_is_showing(preview, message, expected) -> None:
    assert preview_matches(preview, message) is expected


# --- listing attribution --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.carousell.sg/p/teak-lamp-1328307791/", "1328307791"),
        ("https://www.carousell.sg/p/teak-lamp-1328307791", "1328307791"),
        ("https://www.carousell.sg/p/teak-lamp-1328307791/?ref=share", "1328307791"),
        ("https://www.carousell.sg/p/1328307791/", "1328307791"),
        ("https://www.carousell.sg/u/someone/", None),
        ("", None),
    ],
)
def test_the_listing_id_comes_out_of_the_permalink(url, expected) -> None:
    """The marketplace names a conversation's listing by this id, so it is the join to our items."""
    assert listing_id(url, PATTERN) == expected


def _items(*ids):
    return [
        {"id": f"item_{i}", "listing_urls": {"carousell": f"https://www.carousell.sg/p/x-{pid}/"}}
        for i, pid in enumerate(ids)
    ]


def test_a_conversation_is_matched_to_the_item_with_that_listing_id() -> None:
    assert match_item("222", _items("111", "222"), "carousell", PATTERN) == "item_1"


@pytest.mark.parametrize(
    ("product_id", "items", "reason"),
    [
        ("999", _items("111", "222"), "a listing that is not ours"),
        (None, _items("111"), "a conversation with no listing"),
        ("111", [{"id": "item_x", "listing_urls": {}}], "an item we have not published"),
    ],
)
def test_an_unrecognised_listing_matches_nothing(product_id, items, reason) -> None:
    """A thread on the wrong item would negotiate against the wrong floor."""
    assert match_item(product_id, items, "carousell", PATTERN) is None, reason
