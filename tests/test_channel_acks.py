"""The receipt: what the seller hears the moment something lands, before the pass answers it.

The bug it exists for: Telegram renders a button tap as nothing at all, so a seller who tapped
"Accept S$45" saw a chat identical to the one before they tapped, for the 31-960s a channel pass
takes. They read that as the tap not registering and tapped again.
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


def _ack(store, rows, *, pass_was_active=False):
    return acks._ack_for(store, rows, pass_was_active=pass_was_active)


# --- what it says -------------------------------------------------------------------------------


def test_a_tap_is_receipted_with_the_words_the_seller_tapped(store) -> None:
    text, controls = _ack(store, [_tap()])

    assert "Accept S$45" in text
    assert controls is None


def test_the_wait_is_named_and_never_implied_to_be_quick(store) -> None:
    """A receipt that sets the wrong expectation is worse than none: the seller is then waiting on
    a promise rather than on an unknown. Measured passes run 31.6s to 963s."""
    text, _ = _ack(store, [_tap()])

    assert "minute or two" in text and "sometimes longer" in text
    for implies_speed in ("one sec", "a moment", "shortly", "right away", "just a sec"):
        assert implies_speed not in text.lower()


def test_a_receipt_never_says_what_it_will_do_about_it(store) -> None:
    """It is written before any tool has run. Claiming the outcome here is how a send that never
    happened gets reported as one — the same failure send_reply carries a post-mortem for."""
    text, _ = _ack(store, [_tap(label="Accept S$45")])

    for claim in ("accepted", "i've told", "sent", "done"):
        assert claim not in text.lower().replace("Accept S$45".lower(), "")


def test_a_typed_message_is_receipted_too(store) -> None:
    """Measured, text waits longer than a tap does — median 68.8s against 46.9s — and the seller is
    owed the same honesty about it."""
    text, _ = _ack(store, [_text()])

    assert "minute or two" in text


def test_an_unresolvable_tap_never_echoes_its_raw_token(store) -> None:
    """ "a0" is not words. A receipt quoting it would read as the agent having understood something
    it demonstrably did not."""
    text, _ = _ack(store, [_unresolved_tap()])

    assert "a0" not in text
    assert "can't place" in text or "can't tell" in text


# --- how often ----------------------------------------------------------------------------------


def test_a_double_tap_in_one_batch_earns_exactly_one_receipt(store) -> None:
    """The reported behaviour, from the field: two identical taps a second apart. One pass answers
    both, so two receipts would promise two answers — and two sends in the same second is where
    Telegram's per-chat limit is."""
    text, _ = _ack(store, [_tap(), _tap()])

    assert text.count("Accept S$45") == 1


def test_two_different_taps_in_one_batch_are_receipted_by_the_later_one(store) -> None:
    text, _ = _ack(store, [_tap(label="Accept S$45"), _tap(label="❌ Decline", choice="a2")])

    assert "❌ Decline" in text and "Accept S$45" not in text


def test_a_gallery_of_photos_is_one_arrival_not_five(store) -> None:
    photos = [{"kind": "photo", "text": "", "payload": {}} for _ in range(5)]

    text, _ = _ack(store, photos)

    assert text.count("minute or two") == 1


def test_a_message_arriving_mid_pass_is_not_receipted_twice(store) -> None:
    """They have already been told the agent is working; the same pass sweeps this one too."""
    assert _ack(store, [_text()], pass_was_active=True) is None


def test_a_tap_is_receipted_even_mid_pass(store) -> None:
    """Unlike a typed message, a tap leaves no trace in the chat at all — so the receipt is the only
    evidence it happened, and it names which button."""
    text, _ = _ack(store, [_tap()], pass_was_active=True)

    assert "Accept S$45" in text


def test_nothing_routed_means_nothing_to_say(store) -> None:
    assert _ack(store, []) is None


# --- paused -------------------------------------------------------------------------------------


def test_a_paused_agent_says_so_and_offers_the_way_back(store) -> None:
    """A tap while paused is claimed into a pass the lane will never run. Only /resume ends that
    wait, so the receipt has to name the pause and carry the door."""
    store.set_paused(True, source="test")

    text, controls = _ack(store, [_tap()])

    assert "paused" in text.lower() and "Accept S$45" in text
    assert controls == [(fastpaths.RESUME_LABEL, fastpaths.CB_RESUME)]


def test_a_paused_receipt_does_not_promise_a_wait_it_cannot_keep(store) -> None:
    store.set_paused(True, source="test")

    text, _ = _ack(store, [_tap()])

    assert "minute or two" not in text


def test_a_paused_message_is_receipted_even_mid_pass(store) -> None:
    """The mid-pass quiet rule assumes work is moving. While paused it is not, and the pause is
    exactly the thing they do not know."""
    store.set_paused(True, source="test")

    text, _ = _ack(store, [_text()], pass_was_active=True)

    assert "paused" in text.lower()


# --- delivery -----------------------------------------------------------------------------------


def test_the_receipt_is_queued_so_the_pass_can_see_it(store) -> None:
    """recent_transcript reads notices with no status filter, so queuing puts the receipt in the
    pass's own window straight away — which is what stops the pass acknowledging a second time
    without a word added to the prompt asking it not to."""
    sent = []

    acks.ack_arrival(store, [_tap()], pass_was_active=False, reply=lambda t, c: sent.append(t))

    assert sent == []
    queued = store.list_queued_notices()
    assert len(queued) == 1 and "Accept S$45" in queued[0]["text"]
    # The half that matters: visible to the pass before the drain lane has run.
    assert any("Accept S$45" in entry["text"] for entry in store.recent_transcript(10))


def test_a_paused_receipt_is_sent_directly_because_the_drain_lane_is_not_running(store) -> None:
    store.set_paused(True, source="test")
    sent = []

    acks.ack_arrival(store, [_tap()], pass_was_active=False, reply=lambda t, c: sent.append((t, c)))

    assert len(sent) == 1 and "paused" in sent[0][0].lower()
    assert sent[0][1] == [(fastpaths.RESUME_LABEL, fastpaths.CB_RESUME)]
    assert store.list_queued_notices() == []  # never both


@pytest.mark.parametrize("rows", [[], [_text()]], ids=["nothing-routed", "already-told"])
def test_nothing_is_queued_or_sent_when_no_receipt_is_owed(store, rows) -> None:
    sent = []

    acks.ack_arrival(store, rows, pass_was_active=True, reply=lambda t, c: sent.append(t))

    assert sent == [] and store.list_queued_notices() == []
