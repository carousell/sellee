"""The channel pass's prompt: what this pass is here to do, a recent-transcript window for
turn-to-turn memory, and the pending messages to handle now.

The standing rulebook — voice, the listing flow, the house conventions — is not here; it rides in
the system prompt (see the skills package). This module builds only the part that changes per pass.

Both the transcript and the pending rows are read fresh from durable selly.db rows at build time —
no new state — so a restart rebuilds the same prompt. The window is a bounded conversational
context (capped by count and chars), not a memory system; long-term facts stay behind the tools.
"""

from __future__ import annotations

from selly_agent.proc_tree import PASS_PROMPT_MARKER

# The conversational window caps (F13): the most recent N entries, further trimmed to a char budget.
TRANSCRIPT_WINDOW_LIMIT = 40
TRANSCRIPT_CHAR_CAP = 8000

# The task framing: who is talking, what jobs are on the table, and how to pick between them. This
# is the residue of the legacy intent gate — the routing that survived once the choreography around
# it became code.
_INSTRUCTIONS = (
    "The seller is messaging you over Telegram. Read the messages below and handle them, replying "
    "with send_message.\n"
    "What they'll want, in practice: listing something new (follow the listing flow), answering a "
    "question you escalated to them, changing a setting, or asking how things stand. Work out "
    'which from what they actually wrote — a short reply like "yes" or "80" almost always '
    "answers your own last message, so read the conversation above before treating it as a new "
    "request.\n"
    "Finish one thing before starting another: if a listing is mid-flow, don't begin a second one "
    "alongside it.\n"
    "If a decision is theirs to make — a price call, anything risky, anything only they know — "
    "escalate it instead of guessing."
)


def _format_transcript(transcript: list, char_cap: int, skip_paths=()) -> str:
    """Render the window oldest-first, dropping the oldest entries until it fits the char budget.

    A photo from earlier in the conversation keeps its stored paths here. The flow that lists it can
    span passes — identify now, agree a price next turn, publish after that — and only the pass that
    claimed the photo's row sees it under "messages to handle now", so without this the paths would
    vanish the moment the conversation moved on. Paths already in that block are skipped: it is the
    authoritative copy, and listing the same file twice invites attaching it twice.
    """
    skip = set(skip_paths)
    blocks = []
    for entry in transcript:
        who = "seller" if entry["direction"] == "in" else "you"
        lines = [f"[{who}] {entry['text']}"]
        for path in entry.get("media_paths") or []:
            if path not in skip:
                lines.append(f"     {path}")
        blocks.append("\n".join(lines))
    while blocks and sum(len(b) + 1 for b in blocks) > char_cap:
        blocks.pop(0)
    return "\n".join(blocks)


def transcript_media_paths(transcript: list) -> tuple:
    """Every media path in the window, oldest first, de-duplicated — the files a pass may need to
    look at while a listing flow is still in progress."""
    seen = {}
    for entry in transcript:
        for path in entry.get("media_paths") or []:
            seen[path] = None
    return tuple(seen)


def _format_pending(rows: list) -> str:
    """Each pending row as a line. A photo row carries its stored paths inline — they are already
    in the media store, so they can go straight onto an item; without them the pass can see that
    photos arrived but not use them."""
    out = []
    for i, row in enumerate(rows, start=1):
        if row["kind"] == "photo":
            media = row.get("media_paths") or []
            caption = row.get("text") or ""
            out.append(f"{i}. [{len(media)} photo(s)] {caption}".rstrip())
            for path in media:
                out.append(f"     {path}")
        else:
            out.append(f"{i}. {row.get('text') or ''}")
    return "\n".join(out) if out else "(none)"


def build_channel_prompt(
    claimed_rows: list, transcript: list, settings_block: str | None = None
) -> str:
    """Assemble the pass prompt from the claimed pending rows and the recent-transcript window, plus
    a compact settings block (the LLM's near-context vocabulary for what it can propose). History
    and the work-to-do are clearly separated so a follow-up like "yes, do that" resolves against the
    prior turn without confusing it for a new instruction."""
    parts = [PASS_PROMPT_MARKER, _INSTRUCTIONS]
    if settings_block:
        parts.append(settings_block)
    claimed_media = {path for row in claimed_rows for path in (row.get("media_paths") or [])}
    window = _format_transcript(transcript, TRANSCRIPT_CHAR_CAP, skip_paths=claimed_media)
    if window:
        parts.append("Recent conversation (oldest first, for context):\n" + window)
    parts.append("Messages to handle now:\n" + _format_pending(claimed_rows))
    return "\n\n".join(parts)
