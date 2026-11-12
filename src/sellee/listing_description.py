"""The description a marketplace receives: the item's own, then its on_listing answers under a plain
heading. Composed when publishing, never stored, so the seller's description stays theirs."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

HEADING = "Common questions"
# One trailing section as compose writes it: a heading, then single-line Q/A pairs to the end.
_SECTION = re.compile(r"(?:\A|\n\n)" + re.escape(HEADING) + r"(?:\nQ: [^\n]+\nA: [^\n]+)+\Z")


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _without_section(text: str) -> str:
    # A listing read back from a marketplace already carries one; drop it rather than add another.
    while match := _SECTION.search(text):
        text = text[: match.start()].rstrip()
    return text


def compose(description: str | None, answers: Iterable[Mapping]) -> str:
    """`description` plus a section for `answers` (question/answer rows, oldest first). A question
    answered twice shows once, with its latest answer. No answers returns `description` as is."""
    latest: dict[str, str] = {}
    for row in answers:
        latest[_one_line(row["question"])] = _one_line(row["answer"])
    if not latest:
        return description or ""
    section = "\n".join([HEADING, *(f"Q: {q}\nA: {a}" for q, a in latest.items())])
    own = _without_section((description or "").rstrip())
    return f"{own}\n\n{section}" if own else section


def published_description(store, item: Mapping) -> str:
    """The description to publish `item` with: its own, plus its on_listing answers."""
    return compose(item["description"], store.qa_listing_answers(item["id"]))
