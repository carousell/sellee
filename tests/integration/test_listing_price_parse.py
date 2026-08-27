"""The listings page's price parser, run as JavaScript.

Skipped when there is no node on PATH, because that is the only way to find out what this code
actually does: it runs in the page, and every other test in the suite stubs the browser. Worth the
exception because the parse is where a regional site quietly costs a seller the whole feature —
Carousell renders "Rp1.500.000" on its Indonesian site, and read as a decimal point that is NaN,
which drops every row and reports a seller with nothing listed.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess

import pytest

from sellee.browser.markets.carousell import PARSE_PRICE_JS

node_binary = shutil.which("node")

pytestmark = pytest.mark.skipif(node_binary is None, reason="needs node on PATH to run the parser")

# (rendered price, what it means). The grouped-thousands cases are the ones that motivated the
# parser; the decimal cases are what a naive strip-everything-but-digits got right and must keep.
CASES = [
    ("S$80", 80.0),
    ("S$1,299", 1299.0),
    ("S$1,299.00", 1299.0),
    ("$40.00", 40.0),
    ("RM 1,200.50", 1200.5),
    ("HK$40", 40.0),
    ("NT$1,200", 1200.0),
    ("Rp1.500.000", 1500000.0),
    ("Rp 15.000", 15000.0),
    ("Rp1.299,00", 1299.0),
    ("€1.234,56", 1234.56),
    ("1,5", 1.5),
    ("40", 40.0),
]


def _parse(values) -> list:
    script = (
        f"const parse = {PARSE_PRICE_JS};\n"
        f"console.log(JSON.stringify({json.dumps(values)}.map(parse)));"
    )
    out = subprocess.run(
        [node_binary, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(out.stdout)


def test_prices_parse_the_way_each_site_renders_them() -> None:
    parsed = _parse([text for text, _ in CASES])
    assert [(text, got) for (text, _), got in zip(CASES, parsed)] == [
        (text, want) for text, want in CASES
    ]


def test_a_price_that_is_not_one_is_not_invented() -> None:
    """NaN is the answer the reader turns into `unreadable`, so it must not become a number."""
    parsed = _parse(["", "Free", "S$", "--"])
    assert all(value is None or math.isnan(value) for value in parsed), parsed
