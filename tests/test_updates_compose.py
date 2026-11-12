"""Composing one activity update from whatever the seller has not been told.

The composer is pure: these are golden tests over hand-built ledger rows, one per rule, plus the
store read that hands it the two facts about now it cannot read itself, and one end-to-end run
through a real store that greps the result for the floor.
"""

from __future__ import annotations

from time import time as time_now

import pytest
from tests.conftest import leak_paths

from sellee import updates
from sellee.channel.asks import validate_options
from sellee.config import Config
from sellee.engines import pacing
from sellee.store import Store
from sellee.updates import (
    DECISION_OPTIONS,
    UPDATE_MAX_CHARS,
    BuyerAnsweredFacts,
    BuyersUpdatedFacts,
    OfferDeclinedFacts,
    TellableRecord,
    TellRecord,
    UpdateContext,
    compose_update,
    select_tellable,
)

NOW = 1_000_000.0
MINUTE = 60.0
HOUR = 3600.0

MIXER, TREK, DESK, LAMP, CHAIR = "it_mixer", "it_trek", "it_desk", "it_lamp", "it_chair"
TITLES = {
    MIXER: "KitchenAid mixer",
    TREK: "Trek Emonda 54cm",
    DESK: "IKEA desk",
    LAMP: "Arc floor lamp",
    CHAIR: "Eames chair",
}


def _offer(
    tell_id: int,
    item: str,
    amount: float,
    *,
    age=10 * MINUTE,
    thread="fb:o",
    currency="USD",
    title: str | None = None,
) -> TellRecord:
    facts: OfferDeclinedFacts = {
        "buyer_handle": "dana",
        "amount": amount,
        "currency": currency,
        "item_title": title if title is not None else TITLES[item],
    }
    return {
        "id": tell_id,
        "item_id": item,
        "thread_id": f"{thread}{tell_id}",
        "kind": "offer_declined",
        "decision": True,
        "facts": facts,
        "created_ts": NOW - age,
    }


def _answered(
    tell_id: int,
    item: str,
    *,
    thread: str,
    buyer_said="is it still available?",
    we_said="Yes, it's available. Want to come see it this weekend?",
    first=False,
    age=10 * MINUTE,
    title: str | None = None,
) -> TellRecord:
    facts: BuyerAnsweredFacts = {
        "buyer_handle": "bob",
        "buyer_said": buyer_said,
        "we_said": we_said,
        "first_on_item": first,
        "item_title": title if title is not None else TITLES[item],
    }
    return {
        "id": tell_id,
        "item_id": item,
        "thread_id": thread,
        "kind": "buyer_answered",
        "decision": False,
        "facts": facts,
        "created_ts": NOW - age,
    }


def _updated(
    tell_id: int,
    item: str,
    *,
    buyers=2,
    price_drop=True,
    new_photos=False,
    age=10 * MINUTE,
) -> TellRecord:
    facts: BuyersUpdatedFacts = {
        "buyers": buyers,
        "price_drop": price_drop,
        "new_photos": new_photos,
        "item_title": TITLES[item],
    }
    return {
        "id": tell_id,
        "item_id": item,
        "thread_id": None,
        "kind": "buyers_updated",
        "decision": False,
        "facts": facts,
        "created_ts": NOW - age,
    }


def _ctx(*, live=tuple(TITLES), talking=(), waiting=(), replied=None) -> UpdateContext:
    return {
        "live_item_ids": list(live),
        "talking_thread_ids": list(talking),
        "waiting_thread_ids": list(waiting),
        "replied_ts": dict(replied or {}),
    }


def _compose_selected(tells, ctx: UpdateContext, *, now=NOW):
    return compose_update(select_tellable(tells, ctx, now=now), ctx, now=now)


def _compose(tells, *, talking=(), waiting=None, replied=None):
    """Compose what is tellable, with every thread not talking taken as waiting on its buyer,
    unless `waiting` names them, and every thread replied to just now, unless `replied` says."""
    if waiting is None:
        waiting = [t["thread_id"] for t in tells if t["thread_id"] not in talking]
    if replied is None:
        replied = {t["thread_id"]: NOW for t in tells if t["thread_id"]}
    return _compose_selected(tells, _ctx(talking=talking, waiting=waiting, replied=replied))


def _text(tells, *, talking=(), waiting=None, replied=None) -> str:
    update = _compose(tells, talking=talking, waiting=waiting, replied=replied)
    assert update is not None
    return update.text


# --- the prototype ------------------------------------------------------------------------------


def test_the_prototype_update_with_its_decision_as_buttons() -> None:
    tells = [
        _answered(1, TREK, thread="fb:t1", buyer_said="still available?", age=20 * MINUTE),
        _offer(2, MIXER, 40, age=15 * MINUTE),
        _offer(3, MIXER, 45, age=5 * MINUTE),
    ]

    update = _compose(tells, talking=["fb:t1"])

    assert update is not None
    assert update.text == (
        "💰 KitchenAid mixer — 2 offers ($40, $45), both under your minimum. I said no.\n"
        "🗣 Trek Emonda 54cm — answered a buyer "
        '("still available?" → "Yes, it\'s available…").\n'
        "   Still talking."
    )
    assert update.options == ("Lower my minimum", "Hold")
    assert update.carried_ids == (1, 2, 3)


def test_the_decision_buttons_are_a_valid_ask() -> None:
    assert validate_options(list(DECISION_OPTIONS)) == ["Lower my minimum", "Hold"]


# --- each kind alone ---------------------------------------------------------------------------


def test_one_offer_under_the_minimum() -> None:
    update = _compose([_offer(1, TREK, 500)])
    assert update is not None
    assert update.text == ("💰 Trek Emonda 54cm — an offer of $500, under your minimum. I said no.")
    assert update.options == DECISION_OPTIONS


def test_one_buyer_answered_asks_the_seller_nothing() -> None:
    update = _compose([_answered(1, DESK, thread="fb:d1")])
    assert update is not None
    assert update.text == (
        "🗣 IKEA desk — answered a buyer "
        '("is it still available?" → "Yes, it\'s available…").\n'
        "   Waiting on them."
    )
    assert update.options == ()
    assert update.carried_ids == (1,)


def test_the_first_buyer_on_a_listing_is_named_as_such() -> None:
    update = _compose([_answered(1, DESK, thread="fb:d1", first=True)])
    assert update is not None
    assert update.text.startswith("🗣 IKEA desk — answered its first buyer (")


def test_an_empty_list_composes_nothing() -> None:
    assert _compose([]) is None


def test_a_kind_the_composer_does_not_know_is_neither_told_nor_carried() -> None:
    unknown: TellRecord = {**_offer(9, TREK, 500), "kind": "listing_quiet", "decision": False}
    assert _compose([unknown]) is None
    update = _compose([unknown, _offer(1, TREK, 500)])
    assert update is not None and update.carried_ids == (1,)


def test_a_kind_the_composer_does_not_know_is_retired_not_kept_for_later() -> None:
    unknown: TellRecord = {**_offer(9, TREK, 500), "kind": "listing_quiet", "decision": False}
    selection = select_tellable([unknown, _offer(1, TREK, 500)], _ctx(), now=NOW)
    assert [t["id"] for t in selection.tellable] == [1]
    assert selection.retired_ids == (9,)


def _broken(tell: TellRecord, **facts) -> dict:
    broken: dict = {**tell, "facts": {**tell["facts"], **facts}}
    return broken


@pytest.mark.parametrize(
    "bad",
    [
        _broken(_offer(9, TREK, 500), amount=float("nan")),
        _broken(_offer(9, TREK, 500), amount=float("inf")),
        _broken(_offer(9, TREK, 500), amount="500"),
        {**_offer(9, TREK, 500), "facts": None},
        {**_answered(9, TREK, thread="fb:t9"), "facts": ["not", "a", "record"]},
        _broken(_offer(9, TREK, 500), amount=True),
        _broken(_answered(9, TREK, thread="fb:t9"), buyer_said=5),
        _broken(_answered(9, TREK, thread="fb:t9"), we_said={"text": "hi"}),
        _broken(_answered(9, TREK, thread="fb:t9"), item_title=["x"]),
        _broken(_answered(9, TREK, thread="fb:t9"), first_on_item="yes"),
        _broken(_offer(9, TREK, 500), item_title=5),
        _broken(_offer(9, TREK, 500), currency=840),
    ],
    ids=[
        "nan",
        "infinity",
        "text-amount",
        "no-facts",
        "list-facts",
        "true-amount",
        "number-buyer-said",
        "record-we-said",
        "list-title",
        "text-first-buyer",
        "number-offer-title",
        "number-currency",
    ],
)
def test_a_tell_that_cannot_be_read_is_retired_and_never_blocks_the_rest(bad) -> None:
    good = _offer(1, DESK, 40)

    selection = select_tellable([bad, good], _ctx(), now=NOW)
    update = _compose([bad, good])

    assert selection.retired_ids == (9,)
    assert [t["id"] for t in selection.tellable] == [1]
    assert update is not None
    assert update.text == "💰 IKEA desk — an offer of $40, under your minimum. I said no."
    assert update.carried_ids == (1,)


# --- merging tells on one listing --------------------------------------------------------------


def test_three_offers_on_one_listing_are_all_named() -> None:
    update = _compose([_offer(1, MIXER, 40), _offer(2, MIXER, 45), _offer(3, MIXER, 38)])
    assert update is not None
    assert update.text == (
        "💰 KitchenAid mixer — 3 offers ($40, $45, $38), all under your minimum. I said no."
    )


def test_many_offers_on_one_listing_are_given_as_a_range() -> None:
    amounts = (40, 45, 38, 42, 30)
    update = _compose([_offer(i + 1, MIXER, a) for i, a in enumerate(amounts)])
    assert update is not None
    assert update.text == (
        "💰 KitchenAid mixer — 5 offers ($30 to $45), all under your minimum. I said no."
    )


# --- "I said no." only once the buyer has heard it ---------------------------------------------


def test_an_offer_no_reply_has_gone_out_on_yet_is_not_said_to_be_turned_down() -> None:
    tell = _offer(1, DESK, 40)
    assert _text([tell], replied={}) == "💰 IKEA desk — an offer of $40, under your minimum."


def test_a_reply_sent_before_the_offer_does_not_answer_it() -> None:
    tell = _offer(1, DESK, 40, age=10 * MINUTE)
    replied = {tell["thread_id"]: NOW - HOUR}
    assert _text([tell], replied=replied) == "💰 IKEA desk — an offer of $40, under your minimum."


def test_offers_are_said_to_be_turned_down_only_once_every_one_has_had_a_reply() -> None:
    tells = [_offer(1, MIXER, 40), _offer(2, MIXER, 45)]
    replied = {tells[0]["thread_id"]: NOW}
    assert _text(tells, replied=replied) == (
        "💰 KitchenAid mixer — 2 offers ($40, $45), both under your minimum."
    )


def test_two_buyers_answered_on_one_listing_are_counted_not_quoted() -> None:
    tells = [_answered(1, TREK, thread="fb:t1"), _answered(2, TREK, thread="fb:t2")]
    update = _compose(tells, talking=["fb:t2"])
    assert update is not None
    assert update.text == "🗣 Trek Emonda 54cm — answered 2 buyers. Still talking."


def test_one_buyer_answered_twice_is_one_buyer_quoted_at_the_latest_exchange() -> None:
    tells = [
        _answered(1, TREK, thread="fb:t1", buyer_said="available?", we_said="Yes!"),
        _answered(2, TREK, thread="fb:t1", buyer_said="pickup where?", we_said="Tampines."),
    ]
    update = _compose(tells)
    assert update is not None
    assert update.text == (
        '🗣 Trek Emonda 54cm — answered a buyer ("pickup where?" → "Tampines.").\n'
        "   Waiting on them."
    )


def test_offers_and_answers_on_one_listing_are_one_line_naming_them_all() -> None:
    tells = [
        _answered(1, TREK, thread="fb:t1", age=5 * MINUTE, buyer_said="pedals?", we_said="Yes."),
        _answered(2, DESK, thread="fb:d1", age=1 * MINUTE),
        _offer(3, TREK, 400, age=30 * MINUTE),
    ]
    update = _compose(tells, talking=["fb:t1"])
    assert update is not None
    assert update.text.split("\n")[:2] == [
        "💰 Trek Emonda 54cm — an offer of $400, under your minimum. I said no. "
        'Also answered a buyer ("pedals?" → "Yes.").',
        "   Still talking.",
    ]
    assert update.text.count("Trek Emonda 54cm") == 1
    assert update.text.split("\n")[2].startswith("🗣 IKEA desk — ")


def test_a_listing_with_a_decision_and_an_answer_is_one_line_under_needs_you() -> None:
    tells = [
        _offer(1, TREK, 400),
        _answered(2, TREK, thread="fb:t1"),
        _answered(3, TREK, thread="fb:t2"),
        _answered(4, DESK, thread="fb:d1", age=7 * HOUR),
    ]
    update = _compose(tells)
    assert update is not None
    assert update.text.startswith(
        "While you were away:\n"
        "\n"
        "Needs you:\n"
        "💰 Trek Emonda 54cm — an offer of $400, under your minimum. I said no. "
        "Also answered 2 buyers. Waiting on them.\n"
        "\n"
        "Done for you:\n"
        "🗣 IKEA desk — "
    )


def test_listings_without_a_decision_are_ordered_by_their_latest_tell() -> None:
    tells = [
        _answered(1, TREK, thread="fb:t1", age=30 * MINUTE),
        _answered(2, DESK, thread="fb:d1", age=20 * MINUTE),
    ]
    update = _compose(tells)
    assert update is not None
    assert update.text.startswith("🗣 IKEA desk")


# --- still talking or waiting on them -----------------------------------------------------------


def test_a_listing_is_still_talking_when_any_of_its_buyers_is() -> None:
    tells = [_answered(1, TREK, thread="fb:t1"), _answered(2, TREK, thread="fb:t2")]
    assert _text(tells, talking=["fb:t1"]).endswith("Still talking.")
    assert _text(tells, talking=[]).endswith("Waiting on them.")


def test_a_thread_neither_talking_nor_waiting_says_neither() -> None:
    one = [_answered(1, TREK, thread="fb:t1", buyer_said="gift card ok?", we_said="Let me check.")]
    two = [_answered(1, TREK, thread="fb:t1"), _answered(2, TREK, thread="fb:t2")]
    bare = [_answered(1, TREK, thread="fb:t1", buyer_said="", we_said="")]

    assert _text(one, waiting=[]) == (
        '🗣 Trek Emonda 54cm — answered a buyer ("gift card ok?" → "Let me check.").'
    )
    assert _text(two, waiting=[]) == "🗣 Trek Emonda 54cm — answered 2 buyers."
    assert _text(bare, waiting=[]) == "🗣 Trek Emonda 54cm — answered a buyer."
    assert _text(two, waiting=["fb:t2"]).endswith("answered 2 buyers. Waiting on them.")


# --- the held form ------------------------------------------------------------------------------


def test_three_listings_take_the_sectioned_form() -> None:
    tells = [
        _answered(
            1,
            DESK,
            thread="fb:d1",
            buyer_said="any pickup slots Saturday?",
            we_said="Saturday after 10 works. Which time suits?",
            age=50 * MINUTE,
        ),
        _answered(2, TREK, thread="fb:t1", age=40 * MINUTE),
        _answered(3, TREK, thread="fb:t2", age=30 * MINUTE),
        _offer(4, MIXER, 40, age=20 * MINUTE),
        _offer(5, MIXER, 45, age=10 * MINUTE),
    ]
    update = _compose(tells, talking=["fb:t2"])
    assert update is not None
    assert update.text == (
        "While you were away:\n"
        "\n"
        "Needs you:\n"
        "💰 KitchenAid mixer — 2 offers ($40, $45), both under your minimum. I said no.\n"
        "\n"
        "Done for you:\n"
        "🗣 Trek Emonda 54cm — answered 2 buyers. Still talking.\n"
        "🗣 IKEA desk — answered a buyer "
        '("any pickup slots Saturday?" → "Saturday after 10 works…").\n'
        "   Waiting on them."
    )
    assert update.options == DECISION_OPTIONS
    assert update.carried_ids == (1, 2, 3, 4, 5)


def test_a_tell_held_past_six_hours_takes_the_sectioned_form_even_alone() -> None:
    update = _compose([_answered(1, DESK, thread="fb:d1", age=6 * HOUR + 1)])
    assert update is not None
    assert update.text == (
        "While you were away:\n"
        "\n"
        "Done for you:\n"
        "🗣 IKEA desk — answered a buyer "
        '("is it still available?" → "Yes, it\'s available…").\n'
        "   Waiting on them."
    )


def test_a_tell_just_under_six_hours_keeps_the_short_form() -> None:
    update = _compose([_answered(1, DESK, thread="fb:d1", age=6 * HOUR)])
    assert update is not None
    assert not update.text.startswith("While you were away")


def test_a_section_shows_three_listings_then_counts_the_rest() -> None:
    tells = [
        _offer(i + 1, item, 10 + i, age=(10 - i) * MINUTE)
        for i, item in enumerate((MIXER, TREK, DESK, LAMP, CHAIR))
    ]
    update = _compose(tells)
    assert update is not None
    assert update.text == (
        "While you were away:\n"
        "\n"
        "Needs you:\n"
        "💰 Eames chair — an offer of $14, under your minimum. I said no.\n"
        "💰 Arc floor lamp — an offer of $13, under your minimum. I said no.\n"
        "💰 IKEA desk — an offer of $12, under your minimum. I said no.\n"
        "+2 more"
    )
    assert update.carried_ids == (3, 4, 5)


def test_decisions_counted_in_more_come_back_named_in_the_next_update() -> None:
    tells = [
        _offer(i + 1, item, 10 + i, age=(10 - i) * MINUTE)
        for i, item in enumerate((MIXER, TREK, DESK, LAMP, CHAIR))
    ]
    first = _compose(tells)
    assert first is not None

    later = _compose([t for t in tells if t["id"] not in first.carried_ids])

    assert later is not None
    assert later.text == (
        "💰 Trek Emonda 54cm — an offer of $11, under your minimum. I said no.\n"
        "💰 KitchenAid mixer — an offer of $10, under your minimum. I said no."
    )
    assert later.carried_ids == (1, 2)
    assert later.options == DECISION_OPTIONS


def test_what_was_done_for_the_seller_counted_in_more_is_carried() -> None:
    tells = [
        _answered(i + 1, item, thread=f"fb:{i}", age=(10 - i) * MINUTE)
        for i, item in enumerate((MIXER, TREK, DESK, LAMP, CHAIR))
    ]
    update = _compose(tells)
    assert update is not None
    assert update.text.startswith("While you were away:\n\nDone for you:\n")
    assert update.text.endswith("\n+2 more")
    assert update.carried_ids == (1, 2, 3, 4, 5)


def test_the_sectioned_form_without_a_decision_has_no_buttons() -> None:
    tells = [
        _answered(i + 1, item, thread=f"fb:{i}", age=MINUTE)
        for i, item in enumerate((MIXER, TREK, DESK))
    ]
    update = _compose(tells)
    assert update is not None
    assert update.text.startswith("While you were away:\n\nDone for you:\n")
    assert update.options == ()


# --- quoting ------------------------------------------------------------------------------------


def test_a_quote_is_cut_at_its_first_sentence() -> None:
    said = "Is the desk still available? I can come by tonight."
    tell = _answered(1, DESK, thread="fb:d1", buyer_said=said, we_said="It is.")
    assert '("Is the desk still available?…" → "It is.")' in _text([tell])


@pytest.mark.parametrize(
    ("said", "shown"),
    [
        ("Hi! Is it available?", "Hi! Is it available?"),
        ("Hello. Hi! Is it available? Thanks", "Hello. Hi! Is it available?…"),
        ("Hi! Is it available? I can pay cash.", "Hi! Is it available?…"),
    ],
)
def test_a_short_opener_runs_on_into_what_the_buyer_asked(said: str, shown: str) -> None:
    assert _quoted(said) == shown


def test_a_question_cut_short_keeps_its_question_mark() -> None:
    assert _quoted("Would you take 40? or maybe less") == "Would you take 40?…"
    assert _quoted("Is it scratched?! Pics please") == "Is it scratched?…"


def test_a_long_sentence_is_cut_between_words_within_eighty_characters() -> None:
    long_said = "could you tell me " + "whether the desk " * 10 + "is solid wood"
    tell = _answered(1, DESK, thread="fb:d1", buyer_said=long_said, we_said="Yes.")
    quoted = _text([tell]).split('("', 1)[1].split('" →', 1)[0]
    assert len(quoted) <= 80 and quoted.endswith("…")
    kept = quoted[:-1]
    assert long_said.startswith(kept) and long_said[len(kept)] == " "


def _quoted(buyer_said: str) -> str:
    """What the update shows of the buyer's words."""
    text = _text([_answered(1, DESK, thread="fb:d1", buyer_said=buyer_said, we_said="Sure.")])
    return text.split('("', 1)[1].split('" →', 1)[0]


def test_contact_details_are_never_quoted() -> None:
    said = "pls whatsapp +65 9123 4567 or mail me at dana.k@example.com, see bit.ly/xyz"
    text = _text([_answered(1, DESK, thread="fb:d1", buyer_said=said, we_said="Sure.")])
    assert "9123" not in text and "example.com" not in text and "bit.ly" not in text
    assert '"pls whatsapp [phone] or mail me at [email], see [link]"' in text


@pytest.mark.parametrize(
    "number",
    [
        "(650) 248-0749",
        "+1 (650) 248-0749",
        "+65 9123 - 4567",
        "+65 9123  4567",
        "650.248.0749",
        "555 - 123 - 4567",
        "65 9123 4567",
        "555-123-4567",
        "+65 9123–4567",
    ],
)
def test_a_phone_number_however_it_is_spaced_is_never_quoted(number: str) -> None:
    assert _quoted(f"text me {number} thanks") == "text me [phone] thanks"


@pytest.mark.parametrize(
    ("said", "shown"),
    [
        ("msg me wa.me/6591234567 ok", "msg me [link] ok"),
        ("email bob at gmail dot com", "email [email]"),
        ("bob [at] gmail [dot] com pls", "[email] pls"),
        ("find me on instagram @bob_k", "find me on instagram [handle]"),
        ("venmo me @bobby", "venmo me [handle]"),
        ("email bob @ gmail . com", "email [email]"),
        ("email bob@ gmail.com ok", "email [email] ok"),
        ("mail bob＠gmail.com", "mail [email]"),
        ("dm ＠bobby", "dm [handle]"),
        ("my number is six five zero 2 4 8 0 7 4 9", "my number is [phone]"),
        ("call +65 nine one two three 4567", "call [phone]"),
        ("text ６５０ ２４８ ０７４９", "text [phone]"),
        ("pay venmo: bobk", "pay venmo: [handle]"),
        ("my IG: bob.k_23 thx", "my IG: [handle] thx"),
        ("line id: bobby99", "line id: [handle]"),
        ("insta is bob_k99", "insta is [handle]"),
        ("my ig handle is bob_99 thx", "my ig handle is [handle] thx"),
        ("tg://resolve?domain=bobk now", "[link] now"),
        ("weixin://dl/chat?bobk", "[link]"),
        ("ftp://evil.example/x", "[link]"),
        ("whatsapp://send?phone=6591234567 pls", "[link] pls"),
        ("see https://evil.example/a. thanks", "see [link]. thanks"),
        ("bob@gmail[.]com", "[email]"),
        ("bob[at]gmail[.]com", "[email]"),
        ("bob (at) gmail (.) com", "[email]"),
        ("bob[@]gmail[dot]com", "[email]"),
        ("bob＠gmail。com", "[email]"),
        ("evil。com/x", "[link]"),
    ],
)
def test_links_spelled_emails_and_handles_are_never_quoted(said: str, shown: str) -> None:
    assert _quoted(said) == shown


@pytest.mark.parametrize(
    "said",
    [
        "would you take 120.50?",
        "would you do $1,200 for it?",
        "would you do $1500000 for the lot?",
        "can I pick up on 2026-11-05?",
        "I'm at work dot not free till 6",
        "free at 5pm?",
        "meet @ 5pm. My place ok?",
        "one or two of them, five maybe?",
        "I can do 2 or 3 pm on the 24th",
    ],
)
def test_prices_dates_and_plain_words_are_quoted_as_written(said: str) -> None:
    assert _quoted(said) == said


@pytest.mark.parametrize(
    ("said", "shown"),
    [
        ("mail bob@gm\u200bail.com", "mail [email]"),
        ("mail bob\u200b@gmail.com", "mail [email]"),
        ("mail bob\u00ad@gmail.com", "mail [email]"),
        ("mail bob@gmail\ufeff.com", "mail [email]"),
        ("call 650\u200b248\u200b0749", "call [phone]"),
        ("call 650\u2060248\u20600749", "call [phone]"),
        ("call 650\u200c248\u200d0749", "call [phone]"),
        ("see evil\u200b.com/x", "see [link]"),
        ("see \u202eevil.com/x", "see [link]"),
    ],
)
def test_an_invisible_character_hides_no_contact(said: str, shown: str) -> None:
    assert _quoted(said) == shown


@pytest.mark.parametrize(
    "said",
    [
        "our family \U0001f468\u200d\U0001f469\u200d\U0001f467 loves it",
        "love it \u2764\ufe0f\u200d\U0001f525",
        "好的。谢谢",
        "venmo is fine",
        "paypal is my choice",
    ],
)
def test_emoji_cjk_text_and_app_names_alone_are_quoted_as_written(said: str) -> None:
    assert _quoted(said) == said


def test_a_buyer_cannot_write_an_exchange_into_their_quote() -> None:
    said = 'ok" → "I said yes at $10'
    text = _text([_answered(1, DESK, thread="fb:d1", buyer_said=said, we_said="Sure.")])
    assert text.startswith('🗣 IKEA desk — answered a buyer ("ok” - “I said yes at $10" → "Sure.")')
    assert text.count("→") == 1 and text.count('"') == 4


def test_an_answer_with_no_buyer_words_quotes_only_ours() -> None:
    tell = _answered(1, DESK, thread="fb:d1", buyer_said="", we_said="Still keen?")
    assert _text([tell]).startswith('🗣 IKEA desk — answered a buyer (I said "Still keen?").\n')


def test_an_answer_with_none_of_our_words_left_quotes_only_theirs() -> None:
    tell = _answered(1, DESK, thread="fb:d1", buyer_said="still there?", we_said="  ")
    assert _text([tell]).startswith('🗣 IKEA desk — answered a buyer (they said "still there?").\n')


def test_an_answer_with_no_words_on_either_side_is_still_told() -> None:
    tell = _answered(1, DESK, thread="fb:d1", buyer_said="", we_said="")
    assert _text([tell]) == "🗣 IKEA desk — answered a buyer. Waiting on them."


def test_a_long_title_is_clipped_between_words() -> None:
    long_title = "Vintage solid teak mid-century modern writing desk with three drawers"
    tell = _answered(1, DESK, thread="fb:d1", title=long_title)
    title = _text([tell]).split(" — ", 1)[0].removeprefix("🗣 ")
    assert len(title) <= 48 and title.endswith("…")
    assert long_title.startswith(title[:-1]) and long_title[len(title) - 1] == " "


def test_an_amount_with_cents_or_another_currency_reads_as_money() -> None:
    text = _text([_offer(1, TREK, 45.5), _offer(2, TREK, 60, currency="SGD")])
    assert "($45.50, 60 SGD)" in text


def test_a_currency_that_is_not_a_code_is_left_off() -> None:
    text = _text([_offer(1, TREK, 60, currency="SGD" * 400)])
    assert text == "💰 Trek Emonda 54cm — an offer of 60, under your minimum. I said no."


# --- length ------------------------------------------------------------------------------------


def test_the_longest_possible_update_still_fits_one_message() -> None:
    sentence = "x" * 79 + " " + "y" * 200
    long_title = "T" * 200
    tells = []
    for n in range(30):
        item = f"item{n}"
        tells.append(_offer(2 * n + 1, item, 99999.99, age=7 * HOUR, title=long_title))
        tells.append(
            _answered(
                2 * n + 2,
                item,
                thread=f"fb:{n}",
                buyer_said=sentence,
                we_said=sentence,
                first=True,
                age=7 * HOUR,
                title=long_title,
            )
        )
    update = _compose_selected(tells, _ctx(live=[f"item{n}" for n in range(30)]))
    assert update is not None
    assert len(update.text) <= UPDATE_MAX_CHARS


def test_a_text_past_one_message_drops_whole_lines_and_leaves_them_untold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(updates, "UPDATE_MAX_CHARS", 120)
    tells = [_offer(i + 1, item, 10 + i, age=7 * HOUR) for i, item in enumerate((MIXER, TREK))]

    update = _compose(tells)

    assert update is not None
    assert len(update.text) <= 120
    assert update.text == (
        "While you were away:\n"
        "\n"
        "Needs you:\n"
        "💰 KitchenAid mixer — an offer of $10, under your minimum. I said no.\n"
        "…"
    )
    assert update.carried_ids == (1,)
    assert update.options == DECISION_OPTIONS


def test_a_first_line_too_long_for_one_message_is_clipped_rather_than_sent_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(updates, "UPDATE_MAX_CHARS", 40)
    tells = [_offer(1, MIXER, 10), _answered(2, DESK, thread="fb:d1", age=20 * MINUTE)]

    update = _compose(tells)

    assert update is not None
    assert len(update.text) <= 40
    assert update.text == "💰 KitchenAid mixer — an offer of $10…\n…"
    assert update.carried_ids == (1,)


# --- what is tellable ---------------------------------------------------------------------------


def test_tells_on_a_listing_no_longer_live_are_retired_not_told() -> None:
    tells = [_offer(1, TREK, 500), _answered(2, DESK, thread="fb:d1")]
    selection = select_tellable(tells, _ctx(live=[DESK]), now=NOW)
    assert [t["id"] for t in selection.tellable] == [2]
    assert selection.retired_ids == (1,)


def test_a_tell_older_than_two_days_is_retired_as_stale() -> None:
    tells = [_offer(1, TREK, 500, age=48 * HOUR + 1), _offer(2, TREK, 450, age=48 * HOUR)]
    selection = select_tellable(tells, _ctx(), now=NOW)
    assert [t["id"] for t in selection.tellable] == [2]
    assert selection.retired_ids == (1,)


def test_absent_or_empty_text_facts_still_read() -> None:
    tell: dict = {
        **_answered(1, DESK, thread="fb:d1"),
        "facts": {"buyer_handle": "bob", "buyer_said": None, "we_said": "Sure."},
    }
    selection = select_tellable([tell], _ctx(), now=NOW)
    assert selection.retired_ids == ()
    assert _text([tell]) == '🗣 a listing — answered a buyer (I said "Sure.").\n   Waiting on them.'


def test_a_tell_with_no_listing_is_retired() -> None:
    tell: TellRecord = {**_offer(1, TREK, 500), "item_id": None}
    selection = select_tellable([tell], _ctx(), now=NOW)
    assert selection.tellable == () and selection.retired_ids == (1,)


# --- the store's half: what the composer cannot read for itself --------------------------------

_FAST = Config(reply_delay_sec=(0, 0), interactive_reply_delay_sec=(0, 0))
FLOOR = 73.0


def _live_item(store: Store, title: str) -> dict:
    item = store.create_item(title=title, list_price=100.0, currency="USD")
    store.set_floor(item["id"], FLOOR, "seller")
    store.record_listing_url(
        item["id"], "fb", f"https://www.facebook.com/marketplace/item/{item['id']}"
    )
    return item


def _sell_thread(store: Store, item_id: str, tid: str) -> None:
    store.create_thread(
        thread_id=tid, side="sell", market="fb", counterpart_handle="bob", item_id=item_id
    )


def _reply(store: Store, tid: str, text: str) -> None:
    cfg = pacing.resolve(_FAST, quiet_hours=(0, 0))
    reserved = store.reserve_reply(
        thread_id=tid, kind="reply", text=text, in_msg_id=None, cfg=cfg, autonomous=True
    )
    store.commit_reply(
        intent_id=reserved["intent_id"], thread_id=tid, in_msg_id=None, text=text, kind="reply"
    )


def test_the_context_names_live_listings_only(store: Store) -> None:
    live = _live_item(store, "desk")
    draft = store.create_item(title="lamp", list_price=10.0, currency="USD")
    sold = _live_item(store, "chair")
    _sell_thread(store, sold["id"], "fb:s")
    store.negotiate_confirm_sold(sold["id"], "fb:s")

    ctx = store.update_context(
        item_ids=[live["id"], draft["id"], sold["id"], "missing"], thread_ids=[]
    )

    assert ctx["live_item_ids"] == [live["id"]]


def test_a_listing_whose_urls_are_all_empty_is_not_live(store: Store) -> None:
    taken_down = _live_item(store, "desk")
    with store._db.transaction() as conn:
        conn.execute(
            "UPDATE items SET listing_urls = ? WHERE id = ?", ('{"fb": ""}', taken_down["id"])
        )

    ctx = store.update_context(item_ids=[taken_down["id"]], thread_ids=[])

    assert ctx["live_item_ids"] == []


def _answered_thread(store: Store, item_id: str, tid: str) -> None:
    _sell_thread(store, item_id, tid)
    store.record_inbound(tid, msg_id=f"{tid}-1", text="available?", ts=10.0)
    _reply(store, tid, "Yes!")


def test_the_context_says_where_each_thread_stands(store: Store) -> None:
    item = _live_item(store, "desk")
    for tid in ("fb:quiet", "fb:talking", "fb:held", "fb:escalated", "fb:agreed"):
        _answered_thread(store, item["id"], tid)
    later = time_now() + HOUR
    for tid in ("fb:talking", "fb:held", "fb:escalated", "fb:agreed"):
        store.record_inbound(tid, msg_id=f"{tid}-2", text="can I see it today?", ts=later)
    store.hold_thread("fb:held", "scam_check")
    store.escalate("fb:escalated", open_question="Can they see it today?")
    with store._db.transaction() as conn:
        conn.execute("UPDATE threads SET status = 'agreed' WHERE thread_id = 'fb:agreed'")

    ctx = store.update_context(
        item_ids=[],
        thread_ids=["fb:quiet", "fb:talking", "fb:held", "fb:escalated", "fb:agreed", "fb:missing"],
    )

    assert ctx["talking_thread_ids"] == ["fb:agreed", "fb:talking"]
    assert ctx["waiting_thread_ids"] == ["fb:quiet"]


def test_an_inbound_the_cursor_was_moved_over_is_not_talking(store: Store) -> None:
    item = _live_item(store, "desk")
    _answered_thread(store, item["id"], "fb:away")
    away_ts = time_now() + HOUR
    store.record_inbound("fb:away", msg_id="ooo", text="I am out of office.", ts=away_ts)
    store.mark_relay_answered("fb:away", "ooo", away_ts)

    ctx = store.update_context(item_ids=[], thread_ids=["fb:away"])

    assert ctx["talking_thread_ids"] == []
    assert ctx["waiting_thread_ids"] == ["fb:away"]


def test_a_thread_with_our_reply_still_being_sent_is_talking(store: Store) -> None:
    item = _live_item(store, "desk")
    _answered_thread(store, item["id"], "fb:sending")
    store.record_inbound("fb:sending", msg_id="m2", text="today?", ts=time_now() + HOUR)
    reserved = store.reserve_reply(
        thread_id="fb:sending",
        kind="reply",
        text="Yes, after 5.",
        in_msg_id="m2",
        cfg=pacing.resolve(_FAST, quiet_hours=(0, 0)),
        autonomous=True,
    )
    store.mark_intent_sent_unverified(reserved["intent_id"])

    ctx = store.update_context(item_ids=[], thread_ids=["fb:sending"])

    assert ctx["talking_thread_ids"] == ["fb:sending"]
    assert ctx["waiting_thread_ids"] == []


def test_the_context_says_when_our_latest_reply_went_out(store: Store) -> None:
    item = _live_item(store, "desk")
    _answered_thread(store, item["id"], "fb:answered")
    _sell_thread(store, item["id"], "fb:unanswered")

    ctx = store.update_context(item_ids=[], thread_ids=["fb:answered", "fb:unanswered"])

    assert list(ctx["replied_ts"]) == ["fb:answered"]
    assert ctx["replied_ts"]["fb:answered"] > 0


def test_a_real_offer_is_said_to_be_turned_down_once_the_reply_lands(store: Store) -> None:
    desk = _live_item(store, "IKEA desk")
    _sell_thread(store, desk["id"], "fb:d1")
    store.negotiate_offer(desk["id"], "fb:d1", "dana", 40, config=Config())

    def text() -> str:
        tells = store.untold_tells()
        ctx = store.update_context(item_ids=[desk["id"]], thread_ids=["fb:d1"])
        now = time_now()
        update = compose_update(select_tellable(tells, ctx, now=now), ctx, now=now)
        assert update is not None
        return update.text

    assert text() == "💰 IKEA desk — an offer of $40, under your minimum."
    _reply(store, "fb:d1", "Thanks! I could do $50.")
    assert text().startswith("💰 IKEA desk — an offer of $40, under your minimum. I said no. ")


def test_a_tell_row_whose_facts_do_not_parse_is_retired(store: Store) -> None:
    item = _live_item(store, "desk")
    with store._db.transaction() as conn:
        conn.execute(
            "INSERT INTO tells (item_id, thread_id, kind, decision, facts, created_ts) "
            "VALUES (?, 'fb:x', 'offer_declined', 1, '{not json', ?)",
            (item["id"], NOW),
        )

    tells = store.untold_tells()
    selection = select_tellable(tells, _ctx(live=[item["id"]]), now=NOW)

    assert [t["facts"] for t in tells] == [None]
    assert selection.tellable == ()
    assert selection.retired_ids == tuple(t["id"] for t in tells)


def test_a_real_update_never_carries_the_floor(store: Store) -> None:
    mixer, trek = _live_item(store, "KitchenAid mixer"), _live_item(store, "Trek Emonda 54cm")
    store.negotiate_offer(mixer["id"], "fb:m1", "dana", 40, config=Config())
    store.negotiate_offer(mixer["id"], "fb:m2", "eli", 45, config=Config())
    store.negotiate_offer(trek["id"], "fb:t0", "fay", 65, config=Config())
    _sell_thread(store, trek["id"], "fb:t1")
    store.record_inbound("fb:t1", msg_id="m1", text="is it still there?", ts=10.0)
    _reply(store, "fb:t1", "Thanks for asking! Let me check with the seller.")

    tells = store.untold_tells()
    ctx = store.update_context(
        item_ids=[t["item_id"] for t in tells if t["item_id"]],
        thread_ids=[t["thread_id"] for t in tells if t["thread_id"]],
    )
    selection = select_tellable(tells, ctx, now=max(t["created_ts"] for t in tells))
    update = compose_update(selection, ctx, now=max(t["created_ts"] for t in tells))

    assert update is not None
    assert "2 offers ($40, $45), both under your minimum" in update.text
    assert "an offer of $65" in update.text
    assert set(update.carried_ids) == {t["id"] for t in tells}
    assert leak_paths([update.text, list(update.options)], FLOOR) == []


@pytest.mark.parametrize(
    "shape",
    [OfferDeclinedFacts, BuyerAnsweredFacts, BuyersUpdatedFacts, TellRecord, TellableRecord],
)
def test_no_shape_the_composer_reads_has_a_place_for_the_floor(shape) -> None:
    assert not any("floor" in key or "minimum" in key for key in shape.__annotations__)


# --- buyers told about a change to the listing --------------------------------------------------


def test_buyers_told_about_a_price_drop_is_an_fyi_line() -> None:
    update = _compose([_updated(1, LAMP)])
    assert update is not None
    assert update.text == "📣 Arc floor lamp — let 2 buyers who asked know about the price drop."
    assert update.options == ()
    assert update.carried_ids == (1,)


@pytest.mark.parametrize(
    ("buyers", "price_drop", "new_photos", "said"),
    [
        (1, True, False, "let 1 buyer who asked know about the price drop."),
        (3, False, True, "let 3 buyers who asked know about the new photos."),
        (2, True, True, "let 2 buyers who asked know about the price drop and the new photos."),
    ],
)
def test_the_buyers_told_line_names_what_changed(buyers, price_drop, new_photos, said) -> None:
    tell = _updated(1, LAMP, buyers=buyers, price_drop=price_drop, new_photos=new_photos)
    assert _text([tell]) == f"📣 Arc floor lamp — {said}"


def test_buyers_told_on_one_listing_from_two_marketplaces_are_one_line() -> None:
    tells = [
        _updated(1, LAMP, buyers=2),
        _updated(2, LAMP, buyers=1, price_drop=False, new_photos=True),
    ]
    update = _compose(tells)
    assert update is not None
    assert update.text == (
        "📣 Arc floor lamp — let 3 buyers who asked know about the price drop and the new photos."
    )
    assert update.carried_ids == (1, 2)


def test_buyers_told_join_their_listings_answered_line() -> None:
    tells = [_answered(1, DESK, thread="fb:d1"), _updated(2, DESK)]
    assert _text(tells) == (
        "🗣 IKEA desk — answered a buyer "
        '("is it still available?" → "Yes, it\'s available…").\n'
        "   Waiting on them. Also let 2 buyers who asked know about the price drop."
    )


def test_buyers_told_join_their_listings_offer_line_which_keeps_its_buttons() -> None:
    update = _compose([_offer(1, TREK, 400), _updated(2, TREK, buyers=1)])
    assert update is not None
    assert update.text == (
        "💰 Trek Emonda 54cm — an offer of $400, under your minimum. I said no. "
        "Also let 1 buyer who asked know about the price drop."
    )
    assert update.options == DECISION_OPTIONS


def test_buyers_told_are_done_for_the_seller_in_the_sectioned_form() -> None:
    update = _compose([_updated(1, LAMP, age=7 * HOUR), _offer(2, TREK, 400)])
    assert update is not None
    assert update.text.split("\n")[-2:] == [
        "Done for you:",
        "📣 Arc floor lamp — let 2 buyers who asked know about the price drop.",
    ]


@pytest.mark.parametrize(
    "facts",
    [
        {"buyers": 0},
        {"buyers": -1},
        {"buyers": 2.5},
        {"buyers": "2"},
        {"buyers": float("nan")},
        {"buyers": True},
        {"price_drop": "yes"},
        {"new_photos": 1},
        {"price_drop": False, "new_photos": False},
        {"item_title": 5},
    ],
    ids=str,
)
def test_a_buyers_told_tell_that_cannot_be_read_is_retired(facts) -> None:
    bad = _broken(_updated(9, LAMP), **facts)
    selection = select_tellable([bad, _offer(1, TREK, 500)], _ctx(), now=NOW)
    assert [t["id"] for t in selection.tellable] == [1]
    assert selection.retired_ids == (9,)


def test_buyers_told_go_stale_like_any_tell() -> None:
    selection = select_tellable([_updated(1, LAMP, age=49 * HOUR)], _ctx(), now=NOW)
    assert selection.tellable == ()
    assert selection.retired_ids == (1,)
