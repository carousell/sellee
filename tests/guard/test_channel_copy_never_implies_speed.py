"""No string the channel sends may imply a quick turnaround.

The house rule is in skills/voice-and-style.md and it applies to what the daemon writes as much as
to what the model does: a step genuinely takes minutes, so "one sec" sets an expectation nothing can
meet, and a cheery "on it now" followed by two minutes of silence reads as broken.

It used to be guarded by a single assertion inside the arrival receipt's own tests — which meant the
guard was deleted along with the string it was bolted to. Re-homed here, over every literal the
channel sends, it survives the next rewrite and covers the strings that were never checked at all:
it was `fastpaths.CONNECT_CHECK_ACK`'s "one moment while I look" that it found on the first run.

Literals only, never docstrings or comments: this is about what the seller reads, and a comment
arguing *against* a phrase would otherwise trip its own rule.
"""

from __future__ import annotations

import ast
from pathlib import Path

CHANNEL = Path(__file__).resolve().parents[2] / "src" / "sellee" / "channel"

# Each has to be a phrase rather than a word: "shortly" is always a promise, but "moment" appears
# inside honest sentences ("the moment the sign-in page is there") where it names an event rather
# than a duration. The list is the one in voice-and-style.md plus the variants that slipped past it
# — "one moment" is not a substring of "a moment", which is exactly how it survived.
BANNED = (
    "one sec",
    "just a sec",
    "a moment",
    "one moment",
    "shortly",
    "right away",
    "in a bit",
    "any second",
)


def _sent_literals(path: Path):
    """Every string literal in a module that is not a docstring, with its line number."""
    tree = ast.parse(path.read_text(), filename=str(path))
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node not in docstrings:
                yield node.value, node.lineno


def test_no_channel_copy_implies_a_quick_turnaround() -> None:
    offenders = []
    for path in sorted(CHANNEL.rglob("*.py")):
        for literal, lineno in _sent_literals(path):
            for phrase in BANNED:
                if phrase in literal.lower():
                    offenders.append(f"{path.relative_to(CHANNEL)}:{lineno} — {phrase!r}")

    assert offenders == [], "copy implying speed:\n" + "\n".join(offenders)
