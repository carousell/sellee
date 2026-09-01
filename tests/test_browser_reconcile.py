"""Reconcile: turning a thread tail into the rows that are not stored yet.

The properties that matter are idempotency (re-reading an unchanged tail inserts nothing), honesty
about our own and the seller's outbound messages (never recorded twice, never re-replied to), and
conservatism when a row cannot be attributed to exactly one listing.
"""

from __future__ import annotations

import pytest

from sellee.browser.markets.carousell import LISTING_ID_PATTERN as PATTERN
from sellee.browser.reconcile import (
    classify_tail,
    contains_outbound,
    listing_id,
    matching_items,
    message_id,
    new_rows,
    normalize,
    preview_matches,
    same_text,
    unreadable_reason,
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


def test_texts_match_exactly_or_as_a_long_truncation() -> None:
    """The tail read caps bubble text, so a long message and its cut-short read-back are the same
    message — while short texts must still match exactly, or "ok" would open "ok, deal"."""
    assert same_text("ok", " OK ")
    assert not same_text("ok", "ok, deal — see you at 5")
    long_message = "measurements, provenance and receipts " * 8
    assert same_text(long_message, long_message[:300])
    assert not same_text("short", "short" + " padding" * 40)


# --- a message the marketplace drew differently ---------------------------------------------


def test_an_emoji_the_page_did_not_give_back_is_still_the_same_message() -> None:
    """The send check reads its own message back off the page. Facebook draws an emoji as an
    element whose innerText is a line break, so a reply carrying one comes back with a newline
    where the character was — and comparing the two said the message had never arrived.

    Live on 2026-09-01: one buyer was sent the same reply three times, every emoji-bearing send in
    the install was unconfirmed and every emoji-free one committed, and the seller was then asked
    to check by hand whether the message the buyer had received three times had arrived.
    """
    sent = "Hi Humberto! Yes, it's still available at $15 \U0001f60a Keen to grab it?"
    # The three ways a renderer can hand it back: as a break, as a gap, as nothing at all.
    assert same_text(sent, "Hi Humberto! Yes, it's still available at $15 \n Keen to grab it?")
    assert same_text(sent, "Hi Humberto! Yes, it's still available at $15 Keen to grab it?")
    assert same_text(sent, "Hi Humberto! Yes, it's still available at $15Keen to grab it?")
    assert same_text(sent, sent)


def test_dropping_what_the_renderer_ate_does_not_merge_different_messages() -> None:
    """The tolerance discards real characters, so it must never be what decides two genuinely
    different messages are one — a wrong yes here commits an intent against a message we never
    sent, and stops the retry that would have sent it."""
    assert not same_text("Yes, $15 \U0001f60a", "No, $25 \U0001f60a")
    assert not same_text("\U0001f60a", "\U0001f44d")
    assert not same_text("", "anything")


# --- idempotency --------------------------------------------------------------------------------


# --- "our message is on the page" -----------------------------------------------------------


def test_our_own_bubble_is_what_counts_as_on_the_page() -> None:
    tail = [_bubble("still available?", "in"), _bubble("yes it is!", "out")]
    assert contains_outbound(tail, "yes it is!") is True
    assert contains_outbound(tail, "Yes  it  is!") is True  # normalized
    assert contains_outbound(tail, "not said at all") is False


def test_the_buyer_quoting_us_back_is_not_us_having_spoken() -> None:
    """Only an outbound bubble is evidence of a send. Accepting either side would let a buyer
    confirm our own message for us just by repeating it."""
    assert contains_outbound([_bubble("same words", "in")], "same words") is False


def test_a_checkout_link_cut_short_by_the_reader_still_counts() -> None:
    """The reader caps a bubble at 300 characters and a checkout-link reply runs past that, so the
    read-back compares a truncated bubble against the full text it sent."""
    link = (
        "All sorted — here's your checkout link: "
        "https://api.carousell.ai/checkout/8a08c727-872d-430c-968e-4978a2cafca1"
        "?listing_id=2313c1ec-da9d-465e-bd89-6f16be050d90 Just tap through to pay securely and "
        "I'll get it packed and shipped to your postal code 😊 (Heads up: this sale is handled by "
        "SELLY for the seller — you'll complete payment and delivery securely at checkout.)"
    )
    assert contains_outbound([_bubble(link[:300], "out")], link) is True


def test_an_empty_or_missing_tail_confirms_nothing() -> None:
    assert contains_outbound([], "anything") is False
    assert contains_outbound(None, "anything") is False


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
    """As the conversation grows, old bubbles leave the tail and must not return: the tail's
    opening aligns with the end of what is stored, so what came before it stays reconciled."""
    recorded = _recorded([("in", "ok"), ("in", "ok")])
    assert new_rows([_bubble("ok"), _bubble("later")], recorded, now=100.0) == [
        {
            "msg_id": message_id("in", "later", 1),
            "direction": "in",
            "text": "later",
            "ts": 100.0,
        }
    ]


def test_a_repeat_of_a_message_that_scrolled_away_is_still_heard() -> None:
    """The dual of the case above. "ok" was said long ago and has left the window; the buyer says
    "ok" again. Counting copies would swallow it — the stored count exceeds anything the tail can
    still show — leaving the buyer stranded with nothing counting as blind."""
    recorded = _recorded([("in", "ok"), ("in", "a"), ("in", "b"), ("in", "c")])
    tail = [_bubble("b"), _bubble("c"), _bubble("ok")]
    rows = new_rows(tail, recorded, now=100.0)
    assert [(row["direction"], row["text"]) for row in rows] == [("in", "ok")]
    assert rows[0]["msg_id"] == message_id("in", "ok", 2)  # its own id — never deduped away


def test_a_burst_larger_than_the_window_records_the_whole_tail() -> None:
    """When nothing stored is still on screen the tail shares no opening with the transcript, and
    every bubble in it is new."""
    recorded = _recorded([("in", "old")])
    tail = [_bubble("one"), _bubble("two")]
    assert [row["text"] for row in new_rows(tail, recorded, now=100.0)] == ["one", "two"]


# --- our own and the seller's outbound messages --------------------------------------------------


def test_a_reply_we_sent_is_not_recorded_twice() -> None:
    """Our committed reply is stored under the send bracket's own id, so the matching bubble must
    reconcile against it by content — otherwise every reply would double-record."""
    recorded = _recorded([("in", "still available?"), ("out", "yes it is!")])
    tail = [_bubble("still available?"), _bubble("yes it is!", "out")]
    assert new_rows(tail, recorded, now=100.0) == []


def test_a_truncated_read_of_a_long_reply_matches_its_stored_row() -> None:
    """The tail artifact caps bubble text, so a long committed reply reads back cut short. It must
    still reconcile against the stored full text — recording the cut as new would invent a manual
    seller reply, and a phantom manual reply silences the thread."""
    long_reply = "here are the details: " + "measurements and provenance " * 12
    recorded = _recorded([("in", "tell me more?"), ("out", long_reply)])
    tail = [_bubble("tell me more?"), _bubble(long_reply[:300], "out")]
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
    assert matching_items("222", _items("111", "222"), "carousell", PATTERN) == ["item_1"]


def test_two_items_claiming_one_listing_are_both_returned() -> None:
    """Kept distinct from "no match" so the caller can name a data problem as one, rather than
    reporting it as an ordinary listing of someone else's."""
    both = _items("222", "222")
    assert matching_items("222", both, "carousell", PATTERN) == ["item_0", "item_1"]


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
    assert matching_items(product_id, items, "carousell", PATTERN) == [], reason


def test_a_list_of_bubbles_is_readable() -> None:
    """The ordinary answer. Including the empty list: a conversation with nothing in it was read
    successfully and holds nothing, which is not the same as one we could not see."""
    assert unreadable_reason([_bubble("hi")]) is None
    assert unreadable_reason([]) is None


def test_an_adapter_that_says_nothing_still_reads_as_unreadable() -> None:
    """A bare null is the old abstain shape and must keep working — an adapter is free to say only
    "I could not see it"."""
    assert unreadable_reason(None) == "the tail reader gave no answer"


def test_an_abstention_carries_what_the_reader_measured() -> None:
    """The whole point of the mapping shape: the reason reaches the event log, and the event log has
    to answer "why was it blind" without anyone taking a screenshot. These are the live numbers from
    2026-08-29, when a window sized to half a screen made Carousell render one column."""
    reason = unreadable_reason(
        {"error": "no_message_list", "panes": 0, "width": 756, "height": 862, "visible": True}
    )
    assert reason.startswith("no_message_list (")
    for measurement in ("width=756", "panes=0", "visible=True", "height=862"):
        assert measurement in reason


def test_an_abstention_with_no_measurements_is_still_named() -> None:
    assert unreadable_reason({"error": "no_message_list"}) == "no_message_list"
    assert unreadable_reason({}) == "unreadable"


def test_an_answer_that_is_not_a_list_is_unreadable_not_empty() -> None:
    """A reader that fell off its own end, or a market artifact returning a string, must never be
    mistaken for a conversation nobody wrote in."""
    assert "not a list" in (unreadable_reason("[]") or "")
    assert "not a list" in (unreadable_reason(0) or "")
