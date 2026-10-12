"""Which option the Facebook publish dropdown picks, run as JavaScript.

Skipped without node on PATH. On 2026-10-08 Facebook's condition menu offered "Used – good",
with an en dash and a lower-case word, and sellee asked for "Used - Good"; every driven listing
gave up there.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from sellee.browser.markets.facebook import CONDITIONS, options_js, place_js

node_binary = shutil.which("node")

pytestmark = pytest.mark.skipif(node_binary is None, reason="needs node on PATH to run the picker")

FACEBOOK_CONDITIONS = ["New", "Used – like new", "Used – good", "Used – fair"]

_MENU = r"""
const rows = %(labels)s.map((text, i) => {
  const marks = {};
  return {
    innerText: text,
    marks,
    setAttribute: (name, value) => { marks[name] = value; },
    getBoundingClientRect: () => ({ width: 200, height: 30, left: 10, top: 40 * i }),
  };
});
global.document = {
  querySelectorAll: (sel) => (sel.includes('role="option"') ? rows : []),
};
"""


def _pick(wanted: str, labels: list) -> dict:
    script = (
        _MENU % {"labels": json.dumps(labels)}
        + f"console.log(JSON.stringify(({options_js(wanted)})()));"
    )
    out = subprocess.run(
        [str(node_binary), "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


@pytest.mark.parametrize(
    ("wanted", "expected"),
    zip(CONDITIONS, FACEBOOK_CONDITIONS, strict=True),
)
def test_each_condition_is_found_in_facebooks_current_wording(wanted, expected) -> None:
    assert _pick(wanted, FACEBOOK_CONDITIONS)["chosen"] == expected


def test_a_dash_does_not_make_a_different_condition_match() -> None:
    assert _pick("Used - Good", ["Used – like new", "Used – fair"])["chosen"] is None


def _place(zip_code: str, labels: list) -> dict:
    script = (
        _MENU % {"labels": json.dumps(labels)}
        + f"({place_js(zip_code)})().then((r) => console.log(JSON.stringify(r)));"
    )
    out = subprocess.run(
        [str(node_binary), "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_the_suggestion_naming_the_zip_is_taken_and_not_a_lookalike() -> None:
    """What Facebook offered for 94103 on 2026-10-08, lookalikes and all."""
    offered = [
        "Huatusco, Mexico\n0 people checked in here",
        "San Francisco, CA, US 94103\nSan Francisco, CA, US · 17 people checked in here",
    ]

    assert _place("94103", offered) == {"chosen": "San Francisco, CA, US 94103", "at": 1}
    assert _place("94110", offered)["chosen"] is None
