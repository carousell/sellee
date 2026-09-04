"""How a control spec lays out into rows — provider-agnostic, so both renderers pack the same way.

A spec is a flat list of `(label, token)` buttons (see `fastpaths._control_spec`, `asks`), and the
providers turn it into an inline keyboard / action rows. Packing sat in the Telegram renderer as a
fixed four-per-row chunk with no notion of how wide a label is, which shipped `/sellee` as
"What needs…", "👀 Watch …", "Disconnect …" three abreast. A seller tapped one of those ellipses and
got the opposite of what they wanted, because the truncated half was the half naming what the button
did.

Width, not count, is the thing that decides. Buttons in a row split the available width evenly, so
what has to fit is the row's WIDEST label in `budget / n` columns — a row of one 20-column label is
fine, a row of four is not. The count cap survives on top of it, because a row of a dozen two-letter
buttons is its own kind of unreadable.

The budget is deliberately a single approximate number rather than per-provider measurement. There
is no font metric to be had from here — a phone, a desktop and a tablet all differ — so this buys
"never renders an ellipsis on the narrowest thing we ship for" and nothing more.
"""

from __future__ import annotations

# Roughly what one Telegram inline-keyboard row fits on a narrow phone at the default font, in
# columns. Tuned so the two-button rows the seller-facing surfaces actually build stay whole, and
# a sentence-length label takes the row to itself.
ROW_BUDGET_COLS = 34

# Even when width allows more. These are tap targets on a phone; past four in a row they are the
# same size as the gaps between them.
MAX_BUTTONS_PER_ROW = 4

# Zero-width directives: a variation selector (emoji vs text presentation) and a ZWJ (glue in a
# composed emoji) draw nothing on their own, so counting them would inflate every emoji label.
_ZERO_WIDTH = frozenset({0xFE0E, 0xFE0F, 0x200D})


def display_width(label: str) -> int:
    """Roughly how many columns `label` draws in.

    `len()` is wrong for the labels this codebase actually uses: an emoji or an arrow draws about
    twice as wide as an ASCII character, and undercounting is exactly what let "👀 Watch me work"
    into a four-across row it could never fit.
    """
    return sum(2 if ord(c) > 0x7F else 1 for c in label if ord(c) not in _ZERO_WIDTH)


def wrap(spec, *, budget: int = ROW_BUDGET_COLS, max_per_row: int = MAX_BUTTONS_PER_ROW) -> list:
    """Pack `spec` into rows, greedily, left to right. Every button is rendered, in order — a door
    the seller cannot reach is worse than a crowded one.

    A button joins the current row only while the row's widest label would still fit its share of
    the budget. A single label wider than the whole budget takes a row alone rather than wedging the
    pack: it will still truncate, but it truncates by itself instead of taking three others with it,
    and the option-label cap in `asks.validate_options` is what keeps that rare.
    """
    rows: list = []
    row: list = []
    widest = 0
    for label, token in spec:
        candidate = max(widest, display_width(label))
        if row and (len(row) >= max_per_row or candidate * (len(row) + 1) > budget):
            rows.append(row)
            row, widest, candidate = [], 0, display_width(label)
        row.append((label, token))
        widest = candidate
    if row:
        rows.append(row)
    return rows
