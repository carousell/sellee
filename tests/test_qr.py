"""QR module: segno's compact writer behind one seam, and terminal-width headroom."""

from __future__ import annotations

from sellee import qr


def test_render_uses_half_block_glyphs_without_color_codes() -> None:
    out = qr.render_terminal("test")
    assert "█" in out
    assert "\x1b" not in out


def test_a_realistic_bind_link_fits_an_80_column_terminal() -> None:
    # A long bot username pushes the QR version up; the render must still fit 80 columns.
    url = "https://t.me/a_rather_long_bot_username_bot?start=" + "x" * 32
    lines = qr.render_terminal(url).splitlines()
    assert all(len(line) <= 80 for line in lines)
    assert len(lines) <= 24
