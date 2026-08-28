"""Telegram-specific command surface: the "/" menu set, and rendering the core's provider-neutral
controls spec into a Telegram inline keyboard. (The welcome copy is core policy —
channel/outbound.py — queued as ordinary notices at bind time.)
"""

from __future__ import annotations

from sellee.channel.telegram.transport import build_inline_keyboard

# The commands this plan handles deterministically — setMyCommands registers exactly these, so the
# "/" menu never advertises a command the daemon can't answer. Slash stripped, lowercase (Telegram's
# own convention). The settings surface / skills rewrite grow this set as they add real commands.
BOT_COMMANDS = [
    {"command": "sellee", "description": "Settings & what needs you"},
    {"command": "status", "description": "What's live and anything waiting on you"},
    {"command": "catchup", "description": "Everything queued for you right now"},
    {"command": "connect", "description": "Sign in to a marketplace"},
    {"command": "watch", "description": "Watch me work, or send me to the background"},
    {"command": "pause", "description": "Pause the agent (it stops acting)"},
    {"command": "resume", "description": "Resume the agent"},
]

# Buttons per keyboard row. A control spec is a handful of buttons, but the marketplace picker is
# as long as the seller's enabled list — and a row of six is unreadable on a phone, where these
# are tapped. Chunking rather than truncating: every button in the spec is always rendered.
MAX_BUTTONS_PER_ROW = 4


def render_controls(spec) -> dict | None:
    """Render the core's (label, token) control spec into an inline keyboard, wrapped onto as many
    rows as it takes, or None when there are no controls (the fast paths that reply with plain
    text)."""
    if not spec:
        return None
    buttons = [(label, token) for label, token in spec]
    rows = [
        buttons[i : i + MAX_BUTTONS_PER_ROW] for i in range(0, len(buttons), MAX_BUTTONS_PER_ROW)
    ]
    return build_inline_keyboard(rows)
