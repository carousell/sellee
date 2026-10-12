"""Which side of a Facebook conversation a message is on, run as JavaScript.

Skipped without node on PATH. The reader runs in the page, so this drives the shipped artifact
against a small DOM built from geometry captured off the live page — the only way to know what it
really answers.

The case this file exists for was captured on 2026-10-02. A new buyer's first message, "Hi boss,
possible to meet somewhere in red line?", sat in a log only 492px wide because the conversation's
details pane was open beside it. The bubble started 64px from the log's left edge and ended 103px
from its right, and the reader's ratio rule (`fromLeft < fromRight * 0.6`) called it centred. A
centred row is a system banner, so it was dropped, the conversation read as empty three visits
running, and the buyer went unanswered. A long message in a narrow pane is ordinary, so the side
cannot rest on the ratio alone.

The dangerous direction is the other one: a centred system line read as something a person said.
So a line with room on both sides stays centred.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from sellee.browser.markets.facebook import CONVERSATION_TAIL_JS

node_binary = shutil.which("node")

pytestmark = pytest.mark.skipif(node_binary is None, reason="needs node on PATH to run the reader")

# The log as captured: 492px wide, starting 376px into the window.
LOG_LEFT, LOG_WIDTH = 376, 492
TITLE = "Chris Jordan · Dyson HushJet Mini Cool Fan - Full Set (Like New)"

_DOM = r"""
const LOG_LEFT = %(left)d, LOG_WIDTH = %(width)d;
const nodes = %(nodes)s;
const log = {
  getBoundingClientRect: () => ({ left: LOG_LEFT, right: LOG_LEFT + LOG_WIDTH, width: LOG_WIDTH,
    height: 502, top: 0, bottom: 502 }),
  getAttribute: (name) =>
    name === 'aria-label' ? 'Messages in conversation titled ' + %(title)s : null,
  parentElement: null,
  querySelectorAll: () => elements,
};
const element = (n) => {
  const button = n.clickable
    ? { getAttribute: (a) => (a === 'role' ? 'button' : null), parentElement: log,
        cursor: 'pointer' }
    : null;
  return {
    innerText: n.text,
    querySelectorAll: () => [],
    getAttribute: () => null,
    parentElement: button || log,
    cursor: 'auto',
    getBoundingClientRect: () => ({
      left: LOG_LEFT + n.fromLeft, right: LOG_LEFT + LOG_WIDTH - n.fromRight,
      width: LOG_WIDTH - n.fromLeft - n.fromRight, height: 20, top: n.y, bottom: n.y + 20 }),
  };
};
const elements = nodes.map(element);
global.getComputedStyle = (el) => ({ cursor: el.cursor || 'auto' });
global.window = { innerWidth: 1200, innerHeight: 900 };
global.location = { pathname: '/messages/t/1/' };
global.document = {
  visibilityState: 'visible',
  body: { innerText: '' },
  querySelector: () => null,
  querySelectorAll: (sel) => (sel === '[role="log"]' ? [log] : []),
};
"""


def _read(nodes: list) -> list:
    script = (
        _DOM
        % {
            "left": LOG_LEFT,
            "width": LOG_WIDTH,
            "nodes": json.dumps(nodes),
            "title": json.dumps(TITLE),
        }
        + f"\n({CONVERSATION_TAIL_JS})().then((r) => console.log(JSON.stringify(r)));"
    )
    out = subprocess.run(
        [node_binary, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(out.stdout)


def _node(text, from_left, from_right, y=100, clickable=False) -> dict:
    return {
        "text": text,
        "fromLeft": from_left,
        "fromRight": from_right,
        "y": y,
        "clickable": clickable,
    }


def _sides(nodes) -> list:
    return [(row["text"], row["side"]) for row in _read(nodes)]


def test_a_long_first_message_in_a_narrow_pane_is_the_buyers() -> None:
    """The conversation as it was captured, Facebook's furniture and all."""
    page = [
        _node("Chris Jordan", 64, 340, y=380),
        _node("Chris Jordan started this chat. View buyer profile", 90, 92, y=400),
        _node("Chris Jordan", 64, 340, y=440),
        _node("Hi boss, possible to meet somewhere in red line?", 64, 103, y=478),
        _node("Send a quick response", 90, 92, y=540),
        _node("Yes, are you interested?", 64, 250, y=580, clickable=True),
    ]

    assert _sides(page) == [("Hi boss, possible to meet somewhere in red line?", "in")]


def test_a_short_message_is_still_read_by_the_ratio() -> None:
    assert _sides([_node("Hi", 64, 380)]) == [("Hi", "in")]
    assert _sides([_node("ok", 400, 16)]) == [("ok", "out")]


def test_a_long_reply_of_ours_in_a_narrow_pane_is_ours() -> None:
    assert _sides([_node("Sure, Jurong East MRT works for me at 7pm", 103, 16)]) == [
        ("Sure, Jurong East MRT works for me at 7pm", "out")
    ]


def test_a_line_with_room_on_both_sides_stays_centred() -> None:
    """A system line Facebook writes across the middle — one the text filter does not know — must
    never be taken for something the buyer said."""
    assert _sides([_node("This listing is now marked as pending", 120, 125)]) == [
        ("This listing is now marked as pending", "center")
    ]


def test_a_line_spanning_the_whole_log_is_not_given_a_side() -> None:
    assert _sides([_node("A notice that wraps the full width of the log", 20, 22)]) == [
        ("A notice that wraps the full width of the log", "center")
    ]
