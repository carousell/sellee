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
    {"command": "pause", "description": "Pause the agent (it stops acting)"},
    {"command": "resume", "description": "Resume the agent"},
]


def render_controls(spec) -> dict | None:
    """Render the core's (label, token) control spec into a single-row inline keyboard, or None
    when there are no controls (the fast paths that reply with plain text)."""
    if not spec:
        return None
    return build_inline_keyboard([[(label, token) for label, token in spec]])
