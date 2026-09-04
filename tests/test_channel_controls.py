"""Control-row layout: packing a (label, token) spec into rows nobody has to guess at.

The bug this exists for: the renderer chunked at a fixed four per row with no notion of how wide a
label is, so `/sellee` shipped "What needs…", "👀 Watch …" and "Disconnect …" side by side. A seller
tapped one of those ellipses and got the opposite of what they wanted — the truncated half was the
half that said what the button did.

So the property under test is never "how many rows" — it is that a rendered row is readable.
"""

from __future__ import annotations

import pytest

from sellee.channel import controls

# The real specs, verbatim from the surfaces that build them.
CONTROL_ROW = [
    ("⏸ Pause", "pause"),
    ("What needs me", "needsme"),
    ("🌙 Work in background", "watch"),
    ("Disconnect Facebook", "fb:rmmkt"),
    ("Disconnect Carousell", "carousell:rmmkt"),
]
ASK_PINNED = [("✅ Accept", "n1:a0"), ("↔️ Counter", "n1:a1"), ("❌ Decline", "n1:a2")]
ASK_AS_SHIPPED = [  # notice 128, the ask that was tapped in the field
    ("Set floor and I'll respond", "n128:a0"),
    ("Accept S$45", "n128:a1"),
    ("Decline this offer", "n128:a2"),
]
SURVEY = [("Yes, manage them", "fb:adoptyes"), ("No thanks", "fb:adoptno")]
SIGN_IN = [("Sign in on desktop", "fb:connectmkt")]


@pytest.mark.parametrize(
    "spec",
    [CONTROL_ROW, ASK_PINNED, ASK_AS_SHIPPED, SURVEY, SIGN_IN],
    ids=["control-row", "ask-pinned", "ask-as-shipped", "survey", "sign-in"],
)
def test_no_row_is_packed_tighter_than_it_can_be_read(spec) -> None:
    """The one invariant. Buttons in a Telegram row split the width evenly, so a row is legible
    only when its WIDEST label still fits its share — which is why the check is on the widest, not
    on the total."""
    for row in controls.wrap(spec):
        widest = max(controls.display_width(label) for label, _token in row)
        assert widest * len(row) <= controls.ROW_BUDGET_COLS


@pytest.mark.parametrize(
    "spec",
    [CONTROL_ROW, ASK_PINNED, ASK_AS_SHIPPED, SURVEY, SIGN_IN],
    ids=["control-row", "ask-pinned", "ask-as-shipped", "survey", "sign-in"],
)
def test_every_button_survives_the_wrap_in_order(spec) -> None:
    """Wrapping, never dropping or reordering: a door the seller cannot reach is worse than a
    crowded one."""
    assert [b for row in controls.wrap(spec) for b in row] == spec


def test_short_labels_still_share_a_row() -> None:
    """Legibility is the goal, not one-per-row. The pinned answers fit across and reading three
    words is easier than reading three rows."""
    assert len(controls.wrap(ASK_PINNED)) == 1


def test_the_labels_that_shipped_truncated_each_get_their_own_row() -> None:
    assert [len(row) for row in controls.wrap(ASK_AS_SHIPPED)] == [1, 1, 1]


def test_the_control_row_no_longer_crowds_four_across() -> None:
    """The reported layout. 'Work in background' and both Disconnects are too wide to share."""
    rows = controls.wrap(CONTROL_ROW)
    assert [[label for label, _ in row] for row in rows] == [
        ["⏸ Pause", "What needs me"],
        ["🌙 Work in background"],
        ["Disconnect Facebook"],
        ["Disconnect Carousell"],
    ]


def test_many_short_buttons_are_still_capped_per_row() -> None:
    """The marketplace picker: short labels would fit a dozen across by width alone, and a dozen
    tap targets in one row is its own unreadable."""
    spec = [(f"m{i}", f"m{i}:connectmkt") for i in range(9)]

    assert [len(row) for row in controls.wrap(spec)] == [4, 4, 1]


def test_max_per_row_one_forces_a_button_per_row() -> None:
    assert [len(row) for row in controls.wrap(ASK_PINNED, max_per_row=1)] == [1, 1, 1]


def test_an_empty_spec_wraps_to_no_rows() -> None:
    assert controls.wrap([]) == []


def test_an_emoji_counts_as_the_two_columns_it_draws() -> None:
    """len() reads '👀 Watch me work' as 15 and the phone draws ~16. Undercounting is what let an
    emoji label sneak into a row it could not fit."""
    assert controls.display_width("👀 Watch me work") == 16
    assert controls.display_width("What needs me") == 13
    # A variation selector and a ZWJ are directives, not glyphs — they draw nothing on their own.
    assert controls.display_width("↔️ Counter") == 10


def test_one_over_wide_label_never_wedges_the_pack() -> None:
    """A label wider than the whole budget still has to render — alone, and still followed by the
    rest of the spec. The ask cap keeps these rare; the renderer must not assume it away."""
    spec = [("x" * 80, "a"), ("ok", "b")]

    assert [[label for label, _ in row] for row in controls.wrap(spec)] == [["x" * 80], ["ok"]]
