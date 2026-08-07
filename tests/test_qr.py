"""QR module: encoding dimensions, deterministic renders, and terminal-width headroom."""

from __future__ import annotations

from selly_agent import qr

# One dark module. Small enough to hand-compute every rendered cell, quiet zone included.
_DOT = ((1,),)


def test_encode_returns_a_square_binary_matrix() -> None:
    matrix = qr.encode("test")
    assert len(matrix) == 21  # the smallest (version 1) QR symbol
    assert all(len(row) == 21 for row in matrix)
    assert {module for row in matrix for module in row} == {0, 1}


def test_half_block_render_is_dark_on_light_whatever_the_theme() -> None:
    out = qr.render_half_block(_DOT, color=True)
    # 1 module + 4 quiet-zone modules each side = 9 wide; 9 rows pad to 10 = 5 half-block lines.
    blank = "\x1b[37;47m▀▀▀▀▀▀▀▀▀\x1b[0m"
    dot = "\x1b[37;47m▀▀▀▀\x1b[30;47m▀\x1b[37;47m▀▀▀▀\x1b[0m"
    assert out.split("\n") == [blank, blank, dot, blank, blank]


def test_half_block_render_without_color_uses_plain_glyphs() -> None:
    out = qr.render_half_block(_DOT, color=False)
    blank = " " * 9
    assert out.split("\n") == [blank, blank, "    ▀    ", blank, blank]
    assert "\x1b" not in out


def test_half_block_render_packs_two_rows_per_line() -> None:
    both = qr.render_half_block(((1,), (1,)), color=False)
    assert both.split("\n")[2] == "    █    "
    lower = qr.render_half_block(((0,), (1,)), color=False)
    assert lower.split("\n")[2] == "    ▄    "


def test_a_realistic_bind_link_fits_an_80_column_terminal() -> None:
    # A long bot username pushes the QR version up; the render must still fit 80 columns.
    url = "https://t.me/a_rather_long_bot_username_bot?start=" + "x" * 32
    out = qr.render_half_block(qr.encode(url), color=False)
    lines = out.split("\n")
    assert all(len(line) <= 80 for line in lines)
    assert len(lines) <= 24
