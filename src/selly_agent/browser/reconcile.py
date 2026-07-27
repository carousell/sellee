"""Turning what the browser saw into durable rows — pure functions, no I/O.

The rule is reconcile, not infer. A thread's tail is read as ground truth and compared against the
rows already stored; whatever is not stored yet is new. "Have we handled this" is row presence and
"has anyone answered" is the last row's direction, so no memo of what a previous read rendered is
kept anywhere — the state that decides is the state that persists.

The message id is derived from content rather than from a platform id, because the chat DOM exposes
none. Identical text is disambiguated by how many copies are already recorded: a buyer who sends
"ok" twice ends up with two rows, and re-reading the same tail inserts nothing either time. Counting
against stored rows regardless of *how* they were stored is what makes our own sent replies and the
seller's manual ones reconcile correctly too — both are already-recorded outbound text, so the
matching bubble is not recorded twice.
"""

from __future__ import annotations

import hashlib
import re

# How many trailing bubbles a tail read returns. Enough to reconcile a burst of messages between two
# lane ticks without paying for the whole conversation on every read.
TAIL_BUBBLES = 8

# Time and date rows rendered between bubbles ("3:18 PM", "Yesterday", "12/07") — never messages.
_TIME_ROW_RE = re.compile(
    r"^(\d{1,2}:\d{2}\s?(AM|PM)?|Yesterday|Today|\d{1,2}/\d{1,2}(/\d{2,4})?)$",
    re.IGNORECASE,
)

# The leading timestamp token in a fused inbox row ("<handle><time><title><preview>", nothing
# between them). The clock alternative is hour-constrained on purpose: with a bare \d{1,2} a
# handle's trailing digit fuses with a single-digit hour into a bogus earlier match
# ("diamond143" + "4:15 PM" matching at "34:15 PM"), which truncated the handle and broke every
# suppression keyed on it.
_TS_RE = re.compile(
    r"((?:1[0-2]|0?[1-9]):\d{2}\s*[AP]M|Today|Yesterday|Sunday|Monday|Tuesday|Wednesday|"
    r"Thursday|Friday|Saturday|\d{1,2}/\d{1,2})"
)

# How much of a message has to appear in an inbox row's preview for them to be the same message.
_PREVIEW_MATCH_CHARS = 40
# A preview cut mid-message matches on the overlap between its tail and the message's opening; this
# is how much overlap is required, so a couple of shared words is never enough on its own.
_PREVIEW_TRUNCATED_OVERLAP = 12


def normalize(text: str) -> str:
    """The comparable form of a message: whitespace collapsed, lowercased, trailing ellipsis gone
    (a preview or a bubble may be rendered truncated)."""
    collapsed = " ".join((text or "").split()).lower()
    return collapsed.rstrip(".…").strip()


def message_id(direction: str, text: str, occurrence: int) -> str:
    digest = hashlib.sha256(normalize(text).encode()).hexdigest()[:12]  # an id, not a security hash
    return f"{direction}|{digest}|{occurrence}"


def classify_tail(rows, cap: int = TAIL_BUBBLES) -> list:
    """The trailing message bubbles, in page order: separators dropped, centred rows dropped.

    A centred row is a system banner or an offer widget, not something anyone said — keeping one
    would mean recording it as a message and, worse, letting it stand as "someone answered".
    """
    bubbles = [
        row
        for row in rows or []
        if isinstance(row, dict)
        and (row.get("text") or "").strip()
        and row.get("side") in ("in", "out")
        and not _TIME_ROW_RE.match(row["text"].strip())
    ]
    return bubbles[-cap:] if cap else bubbles


def split_handle(row_text: str) -> str:
    """The counterpart handle from a fused inbox row's text, or "" when it cannot be read.

    Everything before the row's first timestamp token is the handle. An unreadable handle comes
    back empty rather than guessed: a wrong handle is worse than no thread, since identity is
    what the suppression layers key on.
    """
    text = " ".join((row_text or "").split())
    if not text:
        return ""
    match = _TS_RE.search(text)
    handle = (text[: match.start()] if match else text).strip()
    return handle[:80]


def preview_matches(preview: str, message: str) -> bool:
    """Whether an inbox row's preview is showing this message.

    An inbox row wraps the message in the handle, a timestamp and the listing title, and cuts it off
    at the row's width — so a match is either the message's opening appearing whole in the row, or
    the row ending part-way through it.

    This only ever decides whether to *skip* opening a thread. A wrong answer costs one sweep
    interval of latency and never a stranded buyer, because the periodic full sweep opens every
    active thread regardless of what a preview claimed.
    """
    haystack = normalize(preview)
    needle = normalize(message)
    if not haystack or not needle:
        return False
    if needle[:_PREVIEW_MATCH_CHARS] in haystack:
        return True
    return _truncated_overlap(haystack, needle) >= min(_PREVIEW_TRUNCATED_OVERLAP, len(needle))


def _truncated_overlap(haystack: str, needle: str) -> int:
    """The length of the longest suffix of `haystack` that opens `needle` — how much of the message
    survived the preview's truncation."""
    for size in range(min(len(haystack), len(needle)), 0, -1):
        if haystack.endswith(needle[:size]):
            return size
    return 0


def new_rows(tail, recorded, *, now: float) -> list:
    """The rows in this tail that are not stored yet, in page order.

    `recorded` is every row already stored for the thread, whatever wrote it — our own committed
    replies, a manual reply journaled earlier, previous reads. Counting occurrences of the same text
    against all of them is what makes this idempotent: the second read of an unchanged tail finds
    every bubble already accounted for.

    Timestamps come from the read, not from the page (the chat exposes no per-bubble time), and step
    forward within the batch so the stored order matches what was on screen.
    """
    already: dict = {}
    for row in recorded or []:
        key = (row.get("dir"), normalize(row.get("text") or ""))
        already[key] = already.get(key, 0) + 1

    seen: dict = {}
    out = []
    for bubble in tail:
        direction = bubble["side"]
        text = bubble["text"]
        key = (direction, normalize(text))
        seen[key] = seen.get(key, 0) + 1
        if seen[key] <= already.get(key, 0):
            continue  # this copy is one we have already stored
        out.append(
            {
                "msg_id": message_id(direction, text, seen[key]),
                "direction": direction,
                "text": text,
                "ts": now + len(out) * 0.001,
            }
        )
    return out


def match_listing(row_text: str, items) -> str | None:
    """The one item this inbox row is about, by its title appearing in the row text.

    Exactly one match or nothing. An inbox row carries the listing title, so this is how a buyer's
    first message becomes a thread — but an ambiguous row (no title matched, or two) is left alone
    rather than attached to a guess, because a thread on the wrong item would negotiate against the
    wrong floor.
    """
    haystack = normalize(row_text)
    if not haystack:
        return None
    matched = [
        item["id"]
        for item in items
        if (item.get("title") or "").strip() and normalize(item["title"]) in haystack
    ]
    return matched[0] if len(matched) == 1 else None
