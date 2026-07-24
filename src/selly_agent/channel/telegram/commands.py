"""Telegram-specific command surface: the "/" menu set, the welcome text, and rendering the core's
provider-neutral controls spec into a Telegram inline keyboard.
"""

from __future__ import annotations

from selly_agent.channel.telegram.transport import build_inline_keyboard

# The commands this plan handles deterministically — setMyCommands registers exactly these, so the
# "/" menu never advertises a command the daemon can't answer. Slash stripped, lowercase (Telegram's
# own convention). The settings surface / skills rewrite grow this set as they add real commands.
BOT_COMMANDS = [
    {"command": "selly", "description": "Settings & what needs you"},
    {"command": "status", "description": "What's live and anything waiting on you"},
    {"command": "catchup", "description": "Everything queued for you right now"},
    {"command": "pause", "description": "Pause the agent (it stops acting)"},
    {"command": "resume", "description": "Resume the agent"},
]

WELCOME_TEXT = (
    "You're connected. I'll message you here when a buyer needs a decision or something needs "
    "your call. Send /selly any time to see your settings and what's waiting."
)


def render_controls(spec) -> dict | None:
    """Render the core's (label, token) control spec into a single-row inline keyboard, or None
    when there are no controls (the fast paths that reply with plain text)."""
    if not spec:
        return None
    return build_inline_keyboard([[(label, token) for label, token in spec]])
