"""The channel pass's prompt: interim selling-agent instructions, a recent-transcript window for
turn-to-turn memory, and the pending messages to handle now.

Both the transcript and the pending rows are read fresh from durable selly.db rows at build time —
no new state — so a restart rebuilds the same prompt. The window is a bounded conversational
context (capped by count and chars), not a memory system; long-term facts stay behind the tools.
This interim prompt is throwaway: the skills rewrite replaces it and finalizes the tier.
"""

from __future__ import annotations

from selly_agent.proc_tree import PASS_PROMPT_MARKER

# The conversational window caps (F13): the most recent N entries, further trimmed to a char budget.
TRANSCRIPT_WINDOW_LIMIT = 40
TRANSCRIPT_CHAR_CAP = 8000

_INSTRUCTIONS = (
    "You are the seller's selling agent, and the seller is messaging you over Telegram. Use your "
    "MCP tools to act on their behalf and reply to them with send_message. Keep replies short and "
    "plain. If you can't decide something on your own — a price call, anything risky — escalate it "
    "rather than guessing."
)


def _format_transcript(transcript: list, char_cap: int) -> str:
    """Render the window oldest-first, dropping the oldest lines until it fits the char budget."""
    lines = [f"[{'seller' if e['direction'] == 'in' else 'you'}] {e['text']}" for e in transcript]
    while lines and sum(len(line) + 1 for line in lines) > char_cap:
        lines.pop(0)
    return "\n".join(lines)


def _format_pending(rows: list) -> str:
    out = []
    for i, row in enumerate(rows, start=1):
        if row["kind"] == "photo":
            count = len(row.get("media_paths") or [])
            caption = row.get("text") or ""
            out.append(f"{i}. [{count} photo(s)] {caption}".rstrip())
        else:
            out.append(f"{i}. {row.get('text') or ''}")
    return "\n".join(out) if out else "(none)"


def build_channel_prompt(claimed_rows: list, transcript: list) -> str:
    """Assemble the pass prompt from the claimed pending rows and the recent-transcript window.
    History and the work-to-do are clearly separated so a follow-up like "yes, do that" resolves
    against the prior turn without confusing it for a new instruction."""
    parts = [PASS_PROMPT_MARKER, _INSTRUCTIONS]
    window = _format_transcript(transcript, TRANSCRIPT_CHAR_CAP)
    if window:
        parts.append("Recent conversation (oldest first, for context):\n" + window)
    parts.append("Messages to handle now:\n" + _format_pending(claimed_rows))
    return "\n\n".join(parts)
