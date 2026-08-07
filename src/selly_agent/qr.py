"""QR rendering for the Telegram bind deep link, as terminal half-blocks.

A thin seam over segno so one module owns the dependency. Its compact terminal writer is
colorless: light modules become glyphs, dark ones spaces, so polarity is correct on a dark
terminal theme and inverted on a light one — most scanners read both, and the printed link is
the fallback when one doesn't. The writer draws the QR spec's 4-module quiet zone itself.
"""

from __future__ import annotations

import io

import segno


def render_terminal(data: str) -> str:
    """``data`` as a half-block terminal QR: two modules per character cell, no color codes."""
    buf = io.StringIO()
    segno.make(data, micro=False).terminal(out=buf, compact=True)
    return buf.getvalue()
