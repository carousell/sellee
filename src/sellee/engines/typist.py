"""How a person types a message: which keys, and the pause before each one.

A chat composer is instrumented at the key level — it is how the page renders a draft and emits the
typing indicator — so the timing between keydowns is something the marketplace can measure. Typing
used to go out as one tool call per line, and the page saw every key of a line land 2.5ms after the
last, all day, whatever the message. This decides a person's timing instead: a speed drawn per
message, a spread around it, longer pauses where people pause, and never a zero gap.

Pure: text and a random source in, keystrokes out. The caller does the sleeping.

What can be pressed is decided here too, because the browser answers `Unknown key` for anything
outside the keyboard layout (an accent, an emoji), and a failure halfway through a reply leaves half
of it in the composer. Printable ASCII is pressed; a line break is Shift+Enter, because in most
composers a bare Enter is the send; everything else is entered whole, as text, the way a picker or
an input method delivers it.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# A typing speed drawn per message: people do not type every message at the same rate.
WPM_RANGE = (35.0, 55.0)
CHARS_PER_WORD = 5
# How far one gap wanders from the message's speed — the sigma of a lognormal around it.
KEY_SPREAD = 0.35
# Pauses, as multiples of an ordinary gap: starting a new word, and after a clause or sentence.
AFTER_SPACE = 1.25
AFTER_STOP = (1.8, 3.2)
STOPS = ",.;:!?"
# Now and then a person stops to think at a word boundary.
THINK_CHANCE = 0.05
THINK_PAUSE_SEC = (0.6, 2.2)
# The first key follows the click into the box.
FIRST_KEY_SEC = (0.25, 0.8)
# No gap is shorter than this, whatever the ceiling asks for.
MIN_GAP_SEC = 0.03
# What a whole message may take. Past it the typing gets faster rather than holding the shared tab,
# and every lane waiting on it, for minutes; the floor above still wins, so nothing is truncated.
CEILING_SEC = 90.0
NEWLINE_KEY = "Shift+Enter"


@dataclass(frozen=True)
class Keystroke:
    """One key, or one character entered whole, and how long to wait before it.

    `delay_sec` is measured from the previous keystroke's dispatch, not from its return, so the
    caller subtracts whatever the call itself took.
    """

    key: str
    insert: bool
    delay_sec: float


def plan(text: str, rng, *, ceiling_sec: float = CEILING_SEC) -> tuple[Keystroke, ...]:
    """The keystrokes that type `text`, with a person's pauses between them."""
    clusters = graphemes(text.replace("\r\n", "\n").replace("\r", "\n"))
    if not clusters:
        return ()
    gap = 60.0 / (rng.uniform(*WPM_RANGE) * CHARS_PER_WORD)
    delays = [rng.uniform(*FIRST_KEY_SEC)]
    for previous, _ in zip(clusters, clusters[1:]):
        delays.append(_pause_after(previous, gap, rng))
    scale = _fit(delays, ceiling_sec)
    return tuple(
        Keystroke(
            key=NEWLINE_KEY if cluster == "\n" else cluster,
            insert=cluster != "\n" and not _pressable(cluster),
            delay_sec=max(MIN_GAP_SEC, delay * scale),
        )
        for cluster, delay in zip(clusters, delays)
    )


# Enough halvings to put the scale within a microsecond's worth of a message's total.
_FIT_STEPS = 40


def _fit(delays: list, ceiling_sec: float) -> float:
    """The largest scale, at most 1, that keeps the floored total under the ceiling.

    Scaling and then flooring is not the same as scaling: every gap the scale takes under the floor
    is raised back up, so a plain `ceiling / total` lands a long message above the ceiling again.
    The floored total only grows with the scale, so the scale is found by halving. When even the
    floor alone is over the ceiling, the floor wins and nothing is truncated.
    """

    def floored(scale: float) -> float:
        return sum(max(MIN_GAP_SEC, delay * scale) for delay in delays)

    if floored(1.0) <= ceiling_sec:
        return 1.0
    low, high = 0.0, 1.0
    for _ in range(_FIT_STEPS):
        mid = (low + high) / 2
        if floored(mid) <= ceiling_sec:
            low = mid
        else:
            high = mid
    return low


def _pause_after(previous: str, gap: float, rng) -> float:
    delay = gap * rng.lognormvariate(0.0, KEY_SPREAD)
    if previous in STOPS or previous == "\n":
        delay *= rng.uniform(*AFTER_STOP)
    elif previous == " ":
        delay *= AFTER_SPACE
    if previous in (" ", "\n") and rng.random() < THINK_CHANCE:
        delay += rng.uniform(*THINK_PAUSE_SEC)
    return delay


def _pressable(cluster: str) -> bool:
    return len(cluster) == 1 and " " <= cluster <= "~"


_ZWJ = "‍"
_KEYCAP = "⃣"
_VARIATION_SELECTORS = ("︎", "️")


def _is_regional_indicator(char: str) -> bool:
    return 0x1F1E6 <= ord(char) <= 0x1F1FF


def _extends(char: str) -> bool:
    """Whether `char` belongs to the character before it rather than starting one of its own."""
    code = ord(char)
    return (
        unicodedata.category(char) in ("Mn", "Me", "Mc")
        or char in _VARIATION_SELECTORS
        or char in (_ZWJ, _KEYCAP)
        or 0x1F3FB <= code <= 0x1F3FF  # skin tones
        or 0xE0020 <= code <= 0xE007F  # tag sequences (subdivision flags)
    )


def graphemes(text: str) -> list[str]:
    """Split `text` into what a person sees as characters.

    The standard library has no grapheme segmentation, so this covers what chat actually contains:
    combining accents, emoji with variation selectors, skin tones, keycaps, ZWJ sequences and flag
    pairs. Anything it misses is entered as more than one piece, which still arrives intact.
    """
    clusters: list[str] = []
    for char in text:
        if clusters and (
            _extends(char)
            or clusters[-1].endswith(_ZWJ)
            or (
                _is_regional_indicator(char)
                and len(clusters[-1]) == 1
                and _is_regional_indicator(clusters[-1])
            )
        ):
            clusters[-1] += char
        else:
            clusters.append(char)
    return clusters
