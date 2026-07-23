"""The channel's command surface: the set Telegram shows in its "/" menu, plus the welcome text.

Fast-path routing (answering these deterministically, no LLM) and the `/selly` settings-card render
land alongside these in a later step; this module starts as the single source of the command set so
the bind flow can register it (setMyCommands) the moment a chat binds.
"""

from __future__ import annotations

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
