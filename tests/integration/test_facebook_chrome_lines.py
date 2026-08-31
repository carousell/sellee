"""Telling Facebook's furniture from what a buyer actually said, run as JavaScript.

Skipped without node on PATH — the only way to find out what this code really does, since it runs
in the page and every other test stubs the browser. Worth the exception for the same reason the
price parser gets one: this decision is where a marketplace quietly puts words in a buyer's mouth.

Every string below was captured from a real thread on 2026-09-01, and the bug that prompted it was
live: Facebook renders quick-reply suggestions inside the message log, and the reader was journaling
them as things the buyer had written. Gerry's conversation — one message, "Good evening, is this
still available?" — came back as five, the last of them "Sorry, it's not available.", which is
Facebook's suggested reply and not a word Gerry ever typed.

The chips themselves are excluded structurally (they are clickable, and nothing a buyer sends is),
so what is pinned here is the other half: the lines that are chrome by their *text*, and — the part
that needs a test far more — the real messages that must never be mistaken for chrome.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from sellee.browser.markets.facebook import CHROME_LINE_JS

node_binary = shutil.which("node")

pytestmark = pytest.mark.skipif(node_binary is None, reason="needs node on PATH to run the filter")

# Facebook's own furniture, verbatim from live threads.
CHROME = [
    "Sent",
    "Message sent",
    "Gerry started this chat. View buyer profile",
    "Gerry is waiting for your response.",
    "Send a quick response",
    "Tap a response to send it to the buyer.",
    "You can now rate each other",
    "People may rate one another based on their interactions",
    # Separators between runs of messages.
    "Jul 25, 2026, 8:59 PM",
    "Jul 27, 2026, 2:40 AM",
    "Thursday 10:15pm",
    "Fri 6:30 AM",
]

# Things people really said, in these very conversations. Every one of these must survive.
SAID = [
    "Good evening, is this still available?",
    "Hi Jerry Neo, is this still available?",
    "Yes",
    "$15",
    "$17 u okay",
    "Go ahead",
    "lol",
    "If $17 come lavender",
    "Hey please stop if you can’t give $17",
    "Haha we're back to $15 again! Still gotta hold at $20 for these, deal?",
    "You're all set at $20! Here's your secure checkout link",
    # A bare time is the case the timestamp rule is deliberately narrow for: a buyer answering
    # "what time?" with "8:30pm" is a message, and an obvious \\d{1,2}:\\d{2} would delete it.
    "8:30pm",
    "8:30 PM",
    "10:15",
    # And a message that merely mentions a day or a date.
    "Can I collect on Thursday?",
    "Jul 25 works for me",
    "I sent it on Monday",
]


def _is_chrome(lines) -> list:
    script = (
        f"const isChrome = {CHROME_LINE_JS};\n"
        f"console.log(JSON.stringify({json.dumps(lines)}.map(isChrome)));"
    )
    out = subprocess.run(
        [node_binary, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(out.stdout)


def test_facebooks_own_furniture_is_never_read_as_a_message() -> None:
    verdicts = _is_chrome(CHROME)

    assert [line for line, chrome in zip(CHROME, verdicts) if not chrome] == []


def test_what_a_person_said_is_never_mistaken_for_furniture() -> None:
    """The dangerous direction. A rule too loose journals Facebook's words as the buyer's; one too
    tight deletes something they actually typed, and nobody ever finds out."""
    verdicts = _is_chrome(SAID)

    assert [line for line, chrome in zip(SAID, verdicts) if chrome] == []


def test_a_bare_time_is_a_message_and_a_stamped_one_is_not() -> None:
    """The distinction the timestamp rule exists to draw, stated on its own because it is the one
    place these two sets come closest together."""
    assert _is_chrome(["8:30pm", "Jul 27, 2026, 2:40 AM"]) == [False, True]
