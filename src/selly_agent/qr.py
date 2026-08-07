"""QR rendering for the Telegram bind deep link, as terminal half-blocks.

segno owns the encoding; the renderer here owns the module→output mapping, because it has
requirements a stock writer doesn't meet: it must force dark-on-light with explicit SGR colors
(phone scanners want dark modules on a light field, and most terminals are light-on-dark), and
the output must be a deterministic string a test can pin.

The render draws its own quiet zone — the light margin the QR spec requires around the symbol.
The terminal cannot rely on the surrounding background being light, so the margin has to be
drawn as explicit light cells.
"""

from __future__ import annotations

import segno

# The QR spec's required light margin, in modules, drawn by each renderer.
_QUIET_ZONE = 4

# Half-block cells pack two vertically-stacked modules into one character: the upper half block
# is drawn in the foreground color (top module) over the background color (bottom module).
_UPPER_HALF = "▀"
_SGR_RESET = "\x1b[0m"
_FG = {0: "37", 1: "30"}  # light module -> white, dark -> black
_BG = {0: "47", 1: "40"}

# Monochrome fallback: dark modules become the drawn (light-on-dark, i.e. inverted) glyphs.
_MONO = {(0, 0): " ", (1, 0): "▀", (0, 1): "▄", (1, 1): "█"}


def encode(data: str) -> tuple:
    """The QR module matrix for ``data``: a tuple of rows, each a tuple of 0 (light) / 1 (dark).

    No quiet zone — the renderer adds its own."""
    code = segno.make(data, micro=False)
    return tuple(tuple(row) for row in code.matrix)


def _with_quiet_zone(matrix: tuple) -> list:
    width = len(matrix[0]) + 2 * _QUIET_ZONE
    blank = (0,) * width
    rows = [blank] * _QUIET_ZONE
    rows.extend((0,) * _QUIET_ZONE + row + (0,) * _QUIET_ZONE for row in matrix)
    rows.extend([blank] * _QUIET_ZONE)
    return rows


def render_half_block(matrix: tuple, *, color: bool) -> str:
    """The matrix as terminal text, two modules per character cell.

    With ``color``, every cell carries explicit black/white SGR colors so the symbol is
    dark-on-light whatever the terminal theme. Without (NO_COLOR), plain half-block glyphs on
    the terminal's own colors — inverted on a dark theme, which many scanners still read; the
    printed link is the fallback when they don't.
    """
    rows = _with_quiet_zone(matrix)
    if len(rows) % 2:
        rows.append((0,) * len(rows[0]))  # pad the odd final row with quiet-zone light
    lines = []
    for top, bottom in zip(rows[0::2], rows[1::2]):
        cells = []
        current = None
        for t, b in zip(top, bottom):
            if color:
                sgr = (t, b)
                if sgr != current:
                    cells.append(f"\x1b[{_FG[t]};{_BG[b]}m")
                    current = sgr
                cells.append(_UPPER_HALF)
            else:
                cells.append(_MONO[(t, b)])
        if color:
            cells.append(_SGR_RESET)
        lines.append("".join(cells))
    return "\n".join(lines)
