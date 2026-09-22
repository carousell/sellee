"""The two arrivals a typing indicator would lie about.

Everything else the seller sends is answered by showing rather than telling: the keyboard comes off
a tapped message in the same tick, their own message gets an eyes reaction, and the chat shows
"typing…" until the pass answers. What is left here is the residue — a paused agent, where nothing
is running and nothing will run, and a tap whose ask cannot be placed, where a pass runs but can
never answer it.

The bug the deleted receipt existed for: Telegram renders a button tap as nothing at all, so a
seller who tapped "Accept S$45" saw a chat identical to the one before they tapped, for the 31-960s
a channel pass takes, and tapped again. It is answered now by the keyboard strip, which lands in the
same tick the tap is read rather than a lane later.
"""

from __future__ import annotations

import pytest

from sellee.channel import acks, fastpaths


def _tap(label="Accept S$45", *, notice_id=7, choice="a1"):
    return {
        "kind": "action",
        "text": label,
        "payload": {"ref": f"n{notice_id}", "choice": choice, "answers_notice_id": notice_id},
    }


def _unresolved_tap():
    """A token whose ask could not be found — the row's text is still the raw token."""
    return {"kind": "action", "text": "a0", "payload": {"ref": "n9999", "choice": "a0"}}


def _text(body="is the lamp still available?"):
    return {"kind": "text", "text": body, "payload": {}}


def _ack(store, rows):
    return acks._ack_for(store, rows)


# --- what is no longer said ----------------------------------------------------------------------


def test_a_placeable_tap_says_nothing_because_the_chat_already_shows_the_wait(store) -> None:
    """The tapped keyboard is gone in the same tick and the indicator is lit. A sentence saying so
    as well is the repetition this change exists to remove."""
    assert _ack(store, [_tap()]) is None


def test_a_typed_message_says_nothing(store) -> None:
    assert _ack(store, [_text()]) is None


def test_a_gallery_of_photos_says_nothing(store) -> None:
    photos = [{"kind": "photo", "text": "", "payload": {}} for _ in range(5)]

    assert _ack(store, photos) is None


def test_nothing_routed_means_nothing_to_say(store) -> None:
    assert _ack(store, []) is None


def test_nothing_is_queued_or_sent_for_an_ordinary_arrival(store) -> None:
    sent = []

    acks.ack_arrival(store, [_tap()], pass_id="pass_1", reply=lambda t, c: sent.append(t))

    assert sent == [] and store.list_queued_notices() == []


# --- a tap whose ask cannot be placed -------------------------------------------------------------


def test_an_unresolvable_tap_never_echoes_its_raw_token(store) -> None:
    """ "a0" is not words. A receipt quoting it would read as the agent having understood something
    it demonstrably did not."""
    text, controls = _ack(store, [_unresolved_tap()])

    assert "a0" not in text
    assert "can't place" in text or "can't tell" in text
    assert controls is None


def test_an_unresolvable_tap_is_stamped_with_the_pass_so_the_indicator_goes_dark(store) -> None:
    """Otherwise the chat shows "I can't tell what that was answering" under a live "typing…" — the
    indicator promising in mime exactly what the words are there to disclaim. `typing_target` stops
    once the waiting pass has queued a word, so these words have to count as that pass's."""
    acks.ack_arrival(store, [_unresolved_tap()], pass_id="pass_7", reply=lambda t, c: None)

    assert store.has_notice_for_pass("pass_7")


def test_a_double_tap_in_one_batch_is_judged_by_the_later_one(store) -> None:
    """One pass answers the whole batch, so what matters is whether the live intent can be placed.
    Two identical taps a second apart is the reported behaviour this coalescing exists for."""
    assert _ack(store, [_tap(), _tap()]) is None
    text, _ = _ack(store, [_tap(), _unresolved_tap()])
    assert "can't place" in text


# --- paused --------------------------------------------------------------------------------------


def test_a_paused_agent_says_so_and_offers_the_way_back(store) -> None:
    """A tap while paused is claimed into a pass the lane will never run, and `pulse_typing` refuses
    to light the chat while paused — so with nothing said there would be no signal at all. Only
    /resume ends that wait, so this has to name the pause and carry the door."""
    store.set_paused(True, source="test")

    text, controls = _ack(store, [_tap()])

    assert "paused" in text.lower()
    assert controls == [(fastpaths.RESUME_LABEL, fastpaths.CB_RESUME)]


def test_a_paused_message_says_so_too(store) -> None:
    store.set_paused(True, source="test")

    text, _ = _ack(store, [_text()])

    assert "paused" in text.lower()


def test_a_paused_ack_does_not_promise_a_wait_it_cannot_keep(store) -> None:
    store.set_paused(True, source="test")

    text, _ = _ack(store, [_tap()])

    assert "minute or two" not in text
    for implies_speed in ("one sec", "a moment", "shortly", "right away", "just a sec"):
        assert implies_speed not in text.lower()


def test_an_unresolvable_tap_while_paused_names_the_pause_as_well(store) -> None:
    """It asks the seller to answer in words, and while paused words are no more actionable than
    the tap was — so saying only the first half would send them off to be ignored twice."""
    store.set_paused(True, source="test")

    text, controls = _ack(store, [_unresolved_tap()])

    assert "can't place" in text and "paused" in text.lower()
    assert controls == [(fastpaths.RESUME_LABEL, fastpaths.CB_RESUME)]


# --- delivery ------------------------------------------------------------------------------------


def test_a_paused_ack_is_sent_directly_because_the_drain_lane_is_not_running(store) -> None:
    store.set_paused(True, source="test")
    sent = []

    acks.ack_arrival(store, [_tap()], pass_id=None, reply=lambda t, c: sent.append((t, c)))

    assert len(sent) == 1 and "paused" in sent[0][0].lower()
    assert sent[0][1] == [(fastpaths.RESUME_LABEL, fastpaths.CB_RESUME)]
    assert store.list_queued_notices() == []  # never both


@pytest.mark.parametrize("rows", [[], [_text()], [_tap()]], ids=["nothing", "typed", "tapped"])
def test_nothing_is_queued_or_sent_when_nothing_is_owed(store, rows) -> None:
    sent = []

    acks.ack_arrival(store, rows, pass_id="pass_1", reply=lambda t, c: sent.append(t))

    assert sent == [] and store.list_queued_notices() == []
