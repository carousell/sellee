"""`Marketplace · Buyer · Listing` — the one shape every seller-facing line uses to say which
conversation it is about, and the per-field suppression that keeps it from saying anything twice.
"""

from __future__ import annotations

from sellee.channel import refs


def _thread(store, *, handle="emline", market="carousell", title="White Study Desk"):
    item = store.create_item(title=title, list_price=40.0, currency="SGD") if title else None
    store.create_thread(
        thread_id=f"{market}:t1",
        side="sell",
        market=market,
        counterpart_handle=handle,
        item_id=item["id"] if item else None,
    )
    return f"{market}:t1"


def test_all_three_fields_in_marketplace_buyer_listing_order(store) -> None:
    thread_id = _thread(store, market="fb", handle="Kamruzzaman")
    assert (
        refs.thread_reference(store, thread_id)
        == "Facebook Marketplace · Kamruzzaman · White Study Desk"
    )


def test_a_thread_with_no_item_still_says_where_and_with_whom(store) -> None:
    """The buy side has a want rather than an item, and a seller answering one still needs to be
    told which app the message is in."""
    want = store.create_want(query="teak lamp")
    store.create_thread(
        thread_id="carousell:b1",
        side="buy",
        market="carousell",
        counterpart_handle="emline",
        want_id=want["want_id"],
    )
    assert refs.thread_reference(store, "carousell:b1") == "Carousell · emline"


def test_an_unknown_thread_says_nothing_rather_than_guessing(store) -> None:
    assert refs.thread_reference(store, "carousell:nope") == ""
    assert refs.thread_reference(store, None) == ""


def test_each_field_the_caller_already_names_is_dropped_on_its_own(store) -> None:
    thread_id = _thread(store, handle="Kamruzzaman")
    assert (
        refs.thread_reference(
            store, thread_id, unless_named_in='Kamruzzaman offered $30 on "White Study Desk"'
        )
        == "Carousell"
    )
    assert (
        refs.thread_reference(store, thread_id, unless_named_in="Kamruzzaman asks about delivery")
        == "Carousell · White Study Desk"
    )


def test_a_field_matched_only_inside_a_longer_word_is_kept(store) -> None:
    """The buyer is the field this most matters for: a one-letter handle is inside almost any
    sentence, and a bare substring test would drop it from the asks that never name them."""
    thread_id = _thread(store, handle="b", title="Teak lamp")
    assert (
        refs.thread_reference(store, thread_id, unless_named_in="Accept $70 for the Teak lamp?")
        == "Carousell · b"
    )


def test_a_newline_in_marketplace_text_cannot_stage_a_second_message(store) -> None:
    thread_id = _thread(store, handle="mal\nlory", title="Desk\nlamp")
    reference = refs.thread_reference(store, thread_id)
    assert "\n" not in reference
    assert reference == "Carousell · mal\\nlory · Desk\\nlamp"
