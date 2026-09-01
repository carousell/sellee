"""Which conversation a seller-facing line is about: `Marketplace · Buyer · Listing`.

One shape, built once, because the seller reads these on a phone with several marketplaces
connected and dozens of buyers open at a time. A line that names only the listing — which is what
every one of them named — cannot be acted on: it does not say which app to open, or who to look
for once it is open.

Here rather than in `outbound`, because `fastpaths` needs it too and `outbound` already imports
`fastpaths`; a helper in either would close the loop.
"""

from __future__ import annotations

from sellee import marketplaces, prompt_data

SEPARATOR = " · "


def thread_reference(store, thread_id, *, unless_named_in: str = "") -> str:
    """`Marketplace · Buyer · Listing` for a thread, or "" when there is nothing to say.

    A field the caller's own text already names is dropped, and only that field. The suppression
    this replaces threw away the whole reference because the *title* appeared in the question,
    which is how an ask that named the buyer and the item still failed to say which marketplace it
    was on. Per-field, an ask the model composed as `<buyer> offered $40 on "<title>"` reduces to
    the marketplace alone — the one thing it never says — and nothing is said twice.

    Buyer handle and listing title are marketplace-sourced, so both go through `one_line`: a
    newline in either would stage a second message the agent never wrote.
    """
    if not thread_id:
        return ""
    thread = store.get_thread(thread_id)
    if thread is None:
        return ""
    item = store.get_item(thread["item_id"]) if thread.get("item_id") else None
    parts = [
        marketplaces.display_name(thread["market"]),
        prompt_data.one_line(thread["counterpart_handle"]),
        prompt_data.one_line((item or {}).get("title")),
    ]
    named = (unless_named_in or "").lower()
    return SEPARATOR.join(part for part in parts if part and not _names(named, part.lower()))


def _names(text: str, field: str) -> bool:
    """Whether `text` already says `field` as a whole run, rather than by coincidence.

    A bare substring test is wrong for the field that most needs saying: a one-letter handle is
    inside almost any sentence, so the buyer would be dropped from exactly the asks that never
    name them.
    """
    start = 0
    while True:
        at = text.find(field, start)
        if at < 0:
            return False
        before = text[at - 1] if at else ""
        after = text[at + len(field) : at + len(field) + 1]
        if not before.isalnum() and not after.isalnum():
            return True
        start = at + 1
