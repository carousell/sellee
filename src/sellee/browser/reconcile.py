"""Turning what the browser saw into durable rows — pure functions, no I/O.

The rule is reconcile, not infer. A thread's tail is read as ground truth and compared against the
rows already stored; whatever is not stored yet is new. "Have we handled this" is row presence and
"has anyone answered" is the last row's direction, so no memo of what a previous read rendered is
kept anywhere — the state that decides is the state that persists.

The message id is derived from content rather than from a platform id, because the chat DOM exposes
none. The tail is reconciled as what it is — the conversation's trailing window: the longest run of
stored rows that ends the transcript and opens the tail is the region both sides already agree on,
and only what follows it is new. That alignment is what keeps repeated text honest in both
directions — a copy that scrolled out of the window is not re-inserted, and a buyer repeating
themselves after it scrolled away is still heard. It holds regardless of *how* a row was stored, so
our own sent replies and the seller's manual ones reconcile correctly too — both are
already-recorded outbound text, so the matching bubble is not recorded twice.
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


# A reader may cap how much of a bubble it returns (the tail artifact slices text), so a long
# message and its cut-short read-back must still compare as the same message. Short texts keep
# exact matching: "ok" opening "ok, deal" is a coincidence, not a truncation.
_TRUNCATION_FLOOR = 200


def same_text(left: str, right: str) -> bool:
    """Whether two message texts are the same message, tolerating one being a truncated read of
    the other."""
    return _same_normalized(normalize(left), normalize(right))


def _same_normalized(a: str, b: str) -> bool:
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= _TRUNCATION_FLOOR and longer.startswith(shorter)


def contains_outbound(tail, text: str) -> bool:
    """Whether `text` is on this tail as one of our own (outbound) bubbles.

    The single definition of "our message is on the page", shared by the send read-back and the
    inbox lane's settle pass — two callers asking the same question about the same page, which must
    never be able to disagree. Truncation-tolerant via `same_text`, because the reader caps how much
    of a bubble it returns and a checkout link runs past that cap.

    Only an outbound bubble counts. "No error from the send" is not success, and neither is our text
    appearing somewhere on the page: a buyer quoting us back is not us having spoken.
    """
    return any(
        bubble.get("side") == "out" and same_text(bubble.get("text") or "", text)
        for bubble in tail or []
    )


def message_id(direction: str, text: str, occurrence: int) -> str:
    digest = hashlib.sha256(normalize(text).encode()).hexdigest()[:12]  # an id, not a security hash
    return f"{direction}|{digest}|{occurrence}"


def unreadable_reason(raw) -> str | None:
    """Why a tail read could not be read, or None when it handed back bubbles.

    An adapter abstains when it cannot identify the message list, and an abstention has to be
    distinguishable from an empty conversation — an empty list would claim the conversation is over.
    Both abstention shapes end up here: a bare `null` from an adapter that says nothing about why,
    and a mapping carrying `error` plus whatever it measured, which is the shape worth having.

    The measurements ride along in the reason string because that string is what reaches the event
    log, and the event log is what has to answer "why was it blind" next time. A window too narrow
    for Carousell's two-column inbox and a marketplace that redesigned its chat are the same bare
    `null`; `no_message_list (height=862, panes=0, visible=True, width=756)` is only one of them.
    """
    if raw is None:
        return "the tail reader gave no answer"
    if isinstance(raw, dict):
        reason = str(raw.get("error") or "unreadable")
        detail = ", ".join(f"{key}={value}" for key, value in sorted(raw.items()) if key != "error")
        return f"{reason} ({detail})" if detail else reason
    if not isinstance(raw, list):
        return f"the tail reader answered {type(raw).__name__}, not a list of bubbles"
    return None


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


def listing_id(url: str, pattern: str) -> str | None:
    """The marketplace's own id for a listing, taken out of its permalink.

    This is the join between a conversation and one of our items: the conversation list names the
    listing by id, and our record of the listing is its URL. Matching on the id is exact, where
    matching a title against a preview string never can be.
    """
    if not url or not pattern:
        return None
    found = re.search(pattern, str(url).split("?", 1)[0].strip())
    return found.group(1) if found else None


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
    replies, a manual reply journaled earlier, previous reads. The tail is aligned against it as a
    trailing window: the longest suffix of the stored rows that matches the tail's opening is the
    shared region, and everything after it is new. Counting copies of each text instead would
    swallow a repeat whose earlier copy has scrolled out of the window — the stored count exceeds
    anything the tail can still show, so the buyer's new "ok" would read as already handled.

    Timestamps come from the read, not from the page (the chat exposes no per-bubble time), and step
    forward within the batch so the stored order matches what was on screen.
    """
    tail_keys = [(bubble["side"], normalize(bubble["text"])) for bubble in tail]
    stored_keys = [(row.get("dir"), normalize(row.get("text") or "")) for row in recorded or []]
    overlap = 0
    for size in range(min(len(tail_keys), len(stored_keys)), 0, -1):
        pairs = zip(stored_keys[-size:], tail_keys[:size])
        # Truncation-tolerant: a long stored reply must match its cut-short bubble, or the bubble
        # would be recorded as an outbound message we never wrote — a phantom manual seller reply
        # that silences the thread.
        if all(s[0] == t[0] and _same_normalized(s[1], t[1]) for s, t in pairs):
            overlap = size
            break

    # Occurrence numbering keeps the content-derived ids unique across repeats, and counting from
    # the stored copies keeps them stable across reads.
    counts: dict = {}
    for key in stored_keys:
        counts[key] = counts.get(key, 0) + 1

    out = []
    for bubble, key in zip(tail[overlap:], tail_keys[overlap:]):
        counts[key] = counts.get(key, 0) + 1
        out.append(
            {
                "msg_id": message_id(bubble["side"], bubble["text"], counts[key]),
                "direction": bubble["side"],
                "text": bubble["text"],
                "ts": now + len(out) * 0.001,
            }
        )
    return out


def matching_items(product_id: str | None, items, market: str, pattern: str) -> list:
    """Which of our items a conversation is about, by the marketplace's listing id.

    Returns every match rather than picking one, so the caller can tell "not a listing of ours" from
    "two of our items claim the same listing" — the first is ordinary and the second is a data
    problem worth naming. A thread attached to the wrong item would negotiate against the wrong
    floor, so anything but a single match is left alone either way.
    """
    if not product_id:
        return []
    return [
        item["id"]
        for item in items
        if listing_id((item.get("listing_urls") or {}).get(market, ""), pattern) == product_id
    ]
