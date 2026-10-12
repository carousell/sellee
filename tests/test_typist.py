"""The typist: which keys a message is typed with, and the pause before each.

What a marketplace can measure is the timing between keydowns, so the promises here are about that
timing over every input — never a zero gap, never an unbounded total, a person's speed on ordinary
text — and about the keys themselves: the message that arrives is the message that was written,
and nothing is pressed that the browser cannot press.
"""

from __future__ import annotations

import random
import statistics

from hypothesis import given, settings
from hypothesis import strategies as st

from sellee.engines import typist

SENTENCE = (
    "Hi Gerry, yes it's still available. I can do $60 if you pick it up this weekend, "
    "anytime after 10am works for me."
)


def _text_of(keys) -> str:
    return "".join("\n" if k.key == typist.NEWLINE_KEY else k.key for k in keys)


def _normalized(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


# --- the message that arrives is the message that was written -----------------------------------


@given(st.text(max_size=400), st.integers(min_value=0, max_value=2**32))
def test_the_keys_spell_out_exactly_the_text(text, seed) -> None:
    keys = typist.plan(text, random.Random(seed))

    assert _text_of(keys) == _normalized(text)


@given(st.text(max_size=400), st.integers(min_value=0, max_value=2**32))
def test_only_what_a_keyboard_can_press_is_pressed(text, seed) -> None:
    """Anything else would fail the press — Playwright answers `Unknown key` for `é` — and a
    failure halfway through a reply leaves half a message in the composer."""
    for key in typist.plan(text, random.Random(seed)):
        if key.insert:
            assert key.key != typist.NEWLINE_KEY
        else:
            assert key.key == typist.NEWLINE_KEY or (len(key.key) == 1 and " " <= key.key <= "~")


def test_a_line_break_is_shift_enter_never_a_bare_enter() -> None:
    """In most composers a bare Enter is the send, which is how a two-line reply goes out as
    half of one."""
    keys = typist.plan("first line\nsecond line", random.Random(1))

    assert [k.key for k in keys if not k.key.isprintable() or len(k.key) > 1] == [
        typist.NEWLINE_KEY
    ]


def test_a_tab_is_inserted_because_pressing_it_moves_focus() -> None:
    keys = typist.plan("a\tb", random.Random(1))

    tab = [k for k in keys if k.key == "\t"]
    assert len(tab) == 1 and tab[0].insert


def test_windows_line_endings_are_one_line_break() -> None:
    keys = typist.plan("one\r\ntwo\rthree", random.Random(1))

    assert _text_of(keys) == "one\ntwo\nthree"


def test_nothing_to_type_is_no_keys() -> None:
    assert typist.plan("", random.Random(1)) == ()


# --- characters a person enters whole -------------------------------------------------------------


def test_emoji_sequences_are_entered_whole() -> None:
    """A family, a flag, a skin tone and a keycap are each several code points and one character
    on screen. Entered a code point at a time, the composer would show the pieces."""
    family = "\U0001f468‍\U0001f469‍\U0001f467"
    flag = "\U0001f1f8\U0001f1ec"
    thumbs = "\U0001f44d\U0001f3fd"
    keycap = "1️⃣"
    heart = "❤️"

    for cluster in (family, flag, thumbs, keycap, heart):
        keys = typist.plan(cluster, random.Random(1))
        assert [(k.key, k.insert) for k in keys] == [(cluster, True)], cluster


def test_a_decomposed_accent_is_entered_with_its_letter() -> None:
    keys = typist.plan("café", random.Random(1))

    assert [k.key for k in keys] == ["c", "a", "f", "é"]
    assert keys[-1].insert


def test_two_flags_in_a_row_are_two_characters() -> None:
    keys = typist.plan("\U0001f1f8\U0001f1ec\U0001f1f2\U0001f1fe", random.Random(1))

    assert [k.key for k in keys] == ["\U0001f1f8\U0001f1ec", "\U0001f1f2\U0001f1fe"]


# --- the timing a marketplace can measure ---------------------------------------------------------


@given(st.text(min_size=1, max_size=400), st.integers(min_value=0, max_value=2**32))
def test_there_is_never_a_zero_gap(text, seed) -> None:
    for key in typist.plan(text, random.Random(seed)):
        assert key.delay_sec >= typist.MIN_GAP_SEC


@settings(max_examples=50)
@given(st.text(min_size=1, max_size=4000), st.integers(min_value=0, max_value=2**32))
def test_the_total_stays_under_the_ceiling_unless_the_floor_forbids_it(text, seed) -> None:
    """A long reply types faster rather than holding the browser — and every lane waiting on it —
    for minutes. It never truncates: the floor wins over the ceiling."""
    keys = typist.plan(text, random.Random(seed))
    total = sum(k.delay_sec for k in keys)

    assert total <= max(typist.CEILING_SEC, len(keys) * typist.MIN_GAP_SEC) + 1e-6


@given(st.integers(min_value=0, max_value=2**32))
def test_the_floor_does_not_push_a_long_message_back_over_the_ceiling(seed) -> None:
    """Scaling to the ceiling and then flooring each gap can land above the ceiling again: every
    gap the scale took under the floor is raised back up. Two thousand characters of ordinary
    words is where that happens, well inside what the floor alone allows."""
    keys = typist.plan("word " * 400, random.Random(seed))

    assert sum(k.delay_sec for k in keys) <= typist.CEILING_SEC + 1e-6


@given(st.integers(min_value=0, max_value=2**32))
def test_ordinary_text_is_typed_at_a_persons_speed(seed) -> None:
    """Measured on the gaps inside words, which is where typing speed lives; the pauses at
    spaces, punctuation and thinking are what make the whole slower, as it is for a person."""
    keys = typist.plan(SENTENCE * 2, random.Random(seed))
    inside_words = [
        b.delay_sec for a, b in zip(keys, keys[1:]) if a.key.isalpha() and b.key.isalpha()
    ]
    fastest = 60.0 / (typist.WPM_RANGE[1] * typist.CHARS_PER_WORD)
    slowest = 60.0 / (typist.WPM_RANGE[0] * typist.CHARS_PER_WORD)

    assert fastest * 0.75 <= statistics.median(inside_words) <= slowest * 1.3


def test_the_gaps_are_not_a_constant_to_measure() -> None:
    keys = typist.plan(SENTENCE, random.Random(7))
    gaps = [k.delay_sec for k in keys[1:]]

    assert statistics.pstdev(gaps) > 0.25 * statistics.mean(gaps)


def test_a_sentence_end_is_a_longer_pause_than_a_letter() -> None:
    after_stop, inside = [], []
    for seed in range(40):
        keys = typist.plan(SENTENCE, random.Random(seed))
        for a, b in zip(keys, keys[1:]):
            if a.key in ".,":
                after_stop.append(b.delay_sec)
            elif a.key.isalpha() and b.key.isalpha():
                inside.append(b.delay_sec)

    assert statistics.median(after_stop) > 2 * statistics.median(inside)


def test_the_same_randomness_types_the_same_way() -> None:
    assert typist.plan(SENTENCE, random.Random(3)) == typist.plan(SENTENCE, random.Random(3))
