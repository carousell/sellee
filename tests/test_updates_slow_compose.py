"""Composing the slow tells — a buyer waiting on the seller, a listing nobody wrote about, a sale
the seller is closing themselves — and the weekly line, from hand-built ledger rows.

Golden tests, like the composer's own: the words each kind becomes, where it sits among the rest,
which buttons an update carries when more than one question is untold, and that none of it can
carry the floor.
"""

from __future__ import annotations

import pytest
from tests.conftest import leak_paths

from sellee.channel.asks import validate_options
from sellee.updates import (
    CHECKIN_OPTIONS,
    DECISION_OPTIONS,
    AskWaitingFacts,
    DealCheckinFacts,
    ListingQuietFacts,
    OfferDeclinedFacts,
    TellRecord,
    UpdateContext,
    compose_update,
    compose_weekly,
    select_tellable,
)

NOW = 1_000_000.0
MINUTE = 60.0
HOUR = 3600.0
DAY = 24 * HOUR
FLOOR = 73.0

TREK, DESK, MIXER, LAMP = "it_trek", "it_desk", "it_mixer", "it_lamp"


def _waiting(
    tell_id: int, item: str, title: str, *, waited=14 * HOUR, said="does it come with pedals?"
) -> TellRecord:
    facts: AskWaitingFacts = {
        "buyer_handle": "bob",
        "buyer_said": said,
        "asked_ts": NOW - waited,
        "item_title": title,
    }
    return _row(tell_id, item, "ask_waiting", facts, thread=f"fb:w{tell_id}", decision=True)


def _quiet(tell_id: int, item: str, title: str, *, live_for=9 * DAY) -> TellRecord:
    facts: ListingQuietFacts = {"live_since_ts": NOW - live_for, "item_title": title}
    return _row(tell_id, item, "listing_quiet", facts, thread=None, decision=False)


def _checkin(tell_id: int, item: str, title: str, *, handle="bob_k", age=20 * MINUTE) -> TellRecord:
    facts: DealCheckinFacts = {"buyer_handle": handle, "item_title": title}
    return _row(
        tell_id, item, "deal_checkin", facts, thread=f"fb:d{tell_id}", decision=True, age=age
    )


def _offer(tell_id: int, item: str, title: str, amount: float) -> TellRecord:
    facts: OfferDeclinedFacts = {
        "buyer_handle": "dana",
        "amount": amount,
        "currency": "USD",
        "item_title": title,
    }
    return _row(tell_id, item, "offer_declined", facts, thread=f"fb:o{tell_id}", decision=True)


def _row(tell_id, item, kind, facts, *, thread, decision, age=20 * MINUTE) -> TellRecord:
    return {
        "id": tell_id,
        "item_id": item,
        "thread_id": thread,
        "kind": kind,
        "decision": decision,
        "facts": facts,
        "created_ts": NOW - age,
    }


def _ctx(tells=()) -> UpdateContext:
    """Every tell's thread replied to just now, so offers read as turned down."""
    return {
        "live_item_ids": [TREK, DESK, MIXER, LAMP],
        "talking_thread_ids": [],
        "waiting_thread_ids": [],
        "replied_ts": {t["thread_id"]: NOW for t in tells if t["thread_id"]},
    }


def _compose(tells):
    ctx = _ctx(tells)
    return compose_update(select_tellable(tells, ctx, now=NOW), ctx, now=NOW)


# --- each kind alone -----------------------------------------------------------------------------


def test_a_buyer_waiting_on_the_seller_says_how_long_and_what_they_said() -> None:
    update = _compose([_waiting(1, TREK, "Trek Emonda 54cm")])

    assert update is not None
    assert update.text == (
        '⏳ Trek Emonda 54cm — a buyer has waited 14 hours for your answer ("does it come with '
        "pedals?\").\n   Reply here and I'll take it from there."
    )
    assert update.options == ()  # the answer is theirs to word, so no buttons
    assert update.carried_ids == (1,)


def test_a_long_wait_is_counted_in_days() -> None:
    update = _compose([_waiting(1, TREK, "Trek Emonda 54cm", waited=3 * DAY + HOUR, said="")])

    assert update is not None
    assert update.text.startswith(
        "⏳ Trek Emonda 54cm — a buyer has waited 3 days for your answer."
    )


def test_a_waiting_buyer_is_quoted_without_their_contact_details() -> None:
    update = _compose([_waiting(1, TREK, "Trek", said="call me at 6502480749 ok?")])

    assert update is not None
    assert "6502480749" not in update.text and "[phone]" in update.text


def test_a_quiet_listing_says_how_long_it_has_been_live() -> None:
    update = _compose([_quiet(1, DESK, "IKEA desk")])

    assert update is not None
    assert update.text == (
        "💤 IKEA desk — live 9 days and no buyer has written yet.\n"
        "   Want me to lower the price? Reply with the new one."
    )
    assert update.options == ()


def test_a_deal_check_in_asks_with_the_three_answers() -> None:
    update = _compose([_checkin(1, DESK, "IKEA desk")])

    assert update is not None
    assert update.text == (
        "🤝 IKEA desk — still on it with bob_k? You said you'd close this sale yourself."
    )
    assert update.options == CHECKIN_OPTIONS
    assert list(CHECKIN_OPTIONS) == ["✅ Sold", "💔 Fell through", "⏳ Still on it"]
    assert validate_options(list(CHECKIN_OPTIONS)) == list(CHECKIN_OPTIONS)


def test_a_check_in_names_a_buyer_handle_without_contact_details() -> None:
    update = _compose([_checkin(1, DESK, "IKEA desk", handle="bob@gmail.com\nfake line")])

    assert update is not None
    assert "gmail" not in update.text and "\n" not in update.text


def test_a_check_in_with_no_handle_names_the_buyer_plainly() -> None:
    update = _compose([_checkin(1, DESK, "IKEA desk", handle="")])

    assert update is not None
    assert "still on it with the buyer?" in update.text


# --- one question per update ------------------------------------------------------------------


def test_offers_under_the_minimum_keep_the_buttons_and_a_check_in_waits() -> None:
    """A tap sends back only its label, so the buttons can answer one question: a check-in
    alongside an offer under the minimum waits, untold, for the next update."""
    update = _compose([_offer(1, MIXER, "KitchenAid mixer", 40.0), _checkin(2, DESK, "IKEA desk")])

    assert update is not None
    assert update.options == DECISION_OPTIONS
    assert update.carried_ids == (1,)
    assert "IKEA desk" not in update.text


def test_two_check_ins_go_one_at_a_time_oldest_first() -> None:
    older = _checkin(1, DESK, "IKEA desk", age=2 * HOUR)
    newer = _checkin(2, LAMP, "Arc floor lamp", age=HOUR)

    update = _compose([newer, older])

    assert update is not None
    assert update.carried_ids == (1,)
    assert update.options == CHECKIN_OPTIONS


def test_a_check_in_rides_with_lines_that_ask_nothing_of_the_buttons() -> None:
    update = _compose([_checkin(1, DESK, "IKEA desk"), _quiet(2, LAMP, "Arc floor lamp")])

    assert update is not None
    assert update.carried_ids == (1, 2)
    assert update.options == CHECKIN_OPTIONS
    assert update.text.index("IKEA desk") < update.text.index("Arc floor lamp")


# --- where each kind sits ---------------------------------------------------------------------


def test_the_held_form_puts_what_needs_the_seller_first_and_quiet_listings_last() -> None:
    tells = [
        _quiet(1, LAMP, "Arc floor lamp"),
        _waiting(2, TREK, "Trek Emonda 54cm"),
        _offer(3, MIXER, "KitchenAid mixer", 40.0),
    ]

    update = _compose(tells)

    assert update is not None
    lines = update.text.split("\n")
    assert lines[0] == "While you were away:"
    needs, quiet = lines.index("Needs you:"), lines.index("Quiet listings:")
    assert needs < quiet
    section = "\n".join(lines[needs:quiet])
    assert "KitchenAid mixer" in section and "Trek Emonda 54cm" in section
    assert "Arc floor lamp" in "\n".join(lines[quiet:])
    assert update.carried_ids == (1, 2, 3)


# --- what cannot be read is retired ----------------------------------------------------------


@pytest.mark.parametrize(
    "broken",
    [
        _row(1, TREK, "ask_waiting", {"item_title": "Trek"}, thread="fb:1", decision=True),
        _row(
            1,
            DESK,
            "listing_quiet",
            {"item_title": "Desk", "live_since_ts": "x"},
            thread=None,
            decision=False,
        ),
        _row(
            1,
            DESK,
            "deal_checkin",
            {"buyer_handle": 7, "item_title": "Desk"},
            thread="fb:1",
            decision=True,
        ),
    ],
)
def test_a_slow_tell_missing_what_its_line_reads_is_retired(broken: TellRecord) -> None:
    selection = select_tellable([broken], _ctx(), now=NOW)

    assert selection.tellable == ()
    assert selection.retired_ids == (1,)


# --- the weekly line --------------------------------------------------------------------------


def test_the_weekly_line_names_the_live_listings_and_asks_about_the_price() -> None:
    text = compose_weekly(
        ["Trek Emonda 54cm", "IKEA desk", "Patagonia jacket"],
        buyers=0,
        aged=["Trek Emonda 54cm", "IKEA desk", "Patagonia jacket"],
    )

    assert text == (
        "👋 Still on it: 3 listings live (“Trek Emonda 54cm”, “IKEA desk”, “Patagonia jacket”). "
        "No buyers wrote this week. Want me to lower one? Tell me which and the new price."
    )


def test_the_weekly_line_for_one_listing() -> None:
    text = compose_weekly(["IKEA desk"], buyers=0, aged=["IKEA desk"])

    assert text == (
        "👋 Still on it: 1 listing live (“IKEA desk”). No buyers wrote this week. "
        "Want me to lower it? Tell me the new price."
    )


def test_the_weekly_line_counts_the_listings_it_has_no_room_to_name() -> None:
    text = compose_weekly(["A lamp", "A desk", "A chair", "A bike", "A rug"], buyers=0, aged=[])

    assert text is not None
    assert text.startswith(
        "👋 Still on it: 5 listings live (“A lamp”, “A desk”, “A chair” and 2 more)."
    )


@pytest.mark.parametrize(("buyers", "said"), [(1, "1 buyer wrote"), (3, "3 buyers wrote")])
def test_the_weekly_line_never_says_nobody_wrote_when_someone_did(buyers, said) -> None:
    text = compose_weekly(["IKEA desk"], buyers=buyers, aged=["IKEA desk"])

    assert text == f"👋 Still on it: 1 listing live (“IKEA desk”). {said} this week."


def test_the_weekly_line_offers_to_lower_only_a_listing_live_a_week() -> None:
    text = compose_weekly(["Arc floor lamp", "IKEA desk"], buyers=0, aged=["IKEA desk"])

    assert text == (
        "👋 Still on it: 2 listings live (“Arc floor lamp”, “IKEA desk”). No buyers wrote "
        "this week. Want me to lower “IKEA desk”? Tell me the new price."
    )


def test_the_weekly_line_names_which_of_several_it_offers_to_lower() -> None:
    text = compose_weekly(
        ["Arc floor lamp", "IKEA desk", "Trek Emonda 54cm"],
        buyers=0,
        aged=["IKEA desk", "Trek Emonda 54cm"],
    )

    assert text is not None
    assert text.endswith(
        "Want me to lower one of “IKEA desk”, “Trek Emonda 54cm”? Tell me which and the new price."
    )


def test_the_weekly_line_asks_nothing_about_listings_just_up() -> None:
    text = compose_weekly(["IKEA desk"], buyers=0, aged=[])

    assert text == "👋 Still on it: 1 listing live (“IKEA desk”). No buyers wrote this week."


def test_a_title_with_commas_still_reads_as_one_listing() -> None:
    text = compose_weekly(["Desk, oak, 120cm", "Lamp"], buyers=0, aged=["Desk, oak, 120cm"])

    assert text == (
        "👋 Still on it: 2 listings live (“Desk, oak, 120cm”, “Lamp”). No buyers wrote this "
        "week. Want me to lower “Desk, oak, 120cm”? Tell me the new price."
    )


def test_the_weekly_line_names_a_quiet_listing_when_other_buyers_wrote() -> None:
    """The count is about other listings; the quiet one is told by name rather than lost in it."""
    text = compose_weekly(
        ["Arc floor lamp", "IKEA desk"],
        buyers=1,
        aged=["Arc floor lamp", "IKEA desk"],
        quiet=["IKEA desk"],
    )

    assert text == (
        "👋 Still on it: 2 listings live (“Arc floor lamp”, “IKEA desk”). 1 buyer wrote this "
        "week, none yet about “IKEA desk”. Want me to lower it? Tell me the new price."
    )


def test_no_live_listing_is_no_weekly_line() -> None:
    assert compose_weekly([], buyers=0, aged=[]) is None


def test_a_weekly_title_is_one_clipped_line() -> None:
    text = compose_weekly(["A very long title\nthat runs " + "on " * 30], buyers=0, aged=[])

    assert text is not None
    assert "\n" not in text and "…" in text


# --- the floor ----------------------------------------------------------------------------------


def test_no_slow_line_can_carry_the_floor() -> None:
    tells = [
        _waiting(1, TREK, "Trek Emonda 54cm"),
        _quiet(2, DESK, "IKEA desk"),
        _checkin(3, LAMP, "Arc floor lamp"),
    ]
    for keys in (AskWaitingFacts, ListingQuietFacts, DealCheckinFacts):
        assert not any("floor" in key or "minimum" in key for key in keys.__annotations__)
    update = _compose(tells)
    weekly = compose_weekly(["IKEA desk"], buyers=0, aged=["IKEA desk"])

    assert update is not None
    assert leak_paths(update.text, FLOOR) == [] and leak_paths(weekly, FLOOR) == []
