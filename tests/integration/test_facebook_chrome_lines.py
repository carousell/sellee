"""Telling Facebook's furniture from what a buyer actually said, run as JavaScript.

Skipped without node on PATH — the only way to find out what this code really does, since it runs
in the page and every other test stubs the browser. Worth the exception because this decision is
where a marketplace quietly puts words in a buyer's mouth.

The chips themselves are excluded structurally (they are clickable, and nothing a buyer sends is),
so what is pinned here is the other half: the lines that are chrome by their *text*, and the real
messages that must never be mistaken for chrome.
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
    # Delivery receipts, which Facebook rewrites under a bubble as time passes — the text changes
    # on every read, so a settled conversation keeps looking like it spoke again.
    "Sent 5m ago",
    "Sent 10m ago",
    "Delivered 2 hours ago",
    "Seen 1m ago",
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
    # A bare time must survive: a buyer answering "what time?" with "8:30pm" is a message.
    "8:30pm",
    "8:30 PM",
    "10:15",
    # And a message that merely mentions a day or a date.
    "Can I collect on Thursday?",
    "Jul 25 works for me",
    "I sent it on Monday",
    # The receipt rule ends in "ago", so anything a person says ending that way has to survive it.
    "I sent it ages ago",
    "Sent it yesterday to my brother",
    "I read your message a while ago",
    "seen it going for $30 not long ago",
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
    """The dangerous direction: a rule too tight deletes something they actually typed, and
    nobody ever finds out."""
    verdicts = _is_chrome(SAID)

    assert [line for line, chrome in zip(SAID, verdicts) if chrome] == []


def test_a_bare_time_is_a_message_and_a_stamped_one_is_not() -> None:
    """The distinction the timestamp rule exists to draw, on its own because it is where the two
    sets come closest."""
    assert _is_chrome(["8:30pm", "Jul 27, 2026, 2:40 AM"]) == [False, True]
