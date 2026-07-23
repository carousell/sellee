"""The channel's command surface: the "/" menu set, the welcome text, and the deterministic fast
paths (answered by daemon code, no LLM).

Fast paths are matched on the exact first-word token (`/pause`, not "please /pause" — fuzzy phrasing
stays the LLM's job) or an inline-keyboard callback. `/selly` renders a settings *card*: current
state with values, closing with an invitation to say what to change in plain words — no numbered
menu, no pick-session state. The settings surface plan grows the card with real settings lines and
the approve button; the one control row (pause/resume · what-needs-me) and its callback plumbing
ship here.
"""

from __future__ import annotations

from .telegram import build_inline_keyboard

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

# The commands answered deterministically (exact first-word token). Everything else routes to the
# channel pass.
_FAST_PATH_COMMANDS = frozenset({"/pause", "/resume", "/status", "/catchup", "/selly"})

# Inline-keyboard callback tokens. The control row emits these; the settings-surface plan reuses
# the same callback plumbing for its approve button (a different token, routed there).
CB_PAUSE = "pause"
CB_RESUME = "resume"
CB_NEEDS_ME = "needsme"
_FAST_PATH_CALLBACKS = frozenset({CB_PAUSE, CB_RESUME, CB_NEEDS_ME})


def is_fast_path(event: dict) -> bool:
    """True if `event` (a normalized inbox row's kind/text/payload) is one the daemon answers
    itself. A command matches on its exact first-word token; an action on its callback choice."""
    if event["kind"] == "command":
        return event["text"] in _FAST_PATH_COMMANDS
    if event["kind"] == "action":
        return (event.get("payload") or {}).get("choice") in _FAST_PATH_CALLBACKS
    return False


def handle_fast_path(store, event: dict) -> tuple:
    """Apply a fast path and return (reply_text, reply_markup|None). Pause/resume flip the control
    flag here (the enforcement — gating passes and killing a running one — lives in the pause
    wiring); the reads render from the store. Assumes is_fast_path(event) is True."""
    token = event["text"] if event["kind"] == "command" else event["payload"]["choice"]
    if token in ("/pause", CB_PAUSE):
        store.set_paused(True, source="telegram")
        return "Paused — I won't act on anything until you resume.", _control_row(store)
    if token in ("/resume", CB_RESUME):
        store.set_paused(False, source="telegram")
        return "Resumed — I'm back on.", _control_row(store)
    if token == "/status":
        return render_status(store), None
    if token == "/catchup":
        return render_catchup(store), None
    # /selly and the what-needs-me button both render the settings card + control row.
    return render_settings_card(store), _control_row(store)


def _control_row(store) -> dict:
    """The one surviving inline row: a pause/resume toggle (reflecting current state) plus a
    what-needs-me shortcut."""
    if store.is_paused():
        toggle = ("▶️ Resume", CB_RESUME)
    else:
        toggle = ("⏸ Pause", CB_PAUSE)
    return build_inline_keyboard([[toggle, ("What needs me", CB_NEEDS_ME)]])


def _needs_me_counts(store) -> tuple:
    return store.count_open_escalations(), store.count_queued_notices()


def render_status(store) -> str:
    """A one-glance status line: paused?, and the counts of what's waiting."""
    escalations, notices = _needs_me_counts(store)
    paused = "paused" if store.is_paused() else "running"
    waiting = escalations + notices
    if waiting:
        return f"Status: {paused}. {escalations} decision(s) and {notices} update(s) waiting."
    return f"Status: {paused}. Nothing waiting on you."


def render_catchup(store) -> str:
    """The deep needs-me view: each open escalation's question, then queued updates. This is a
    render only — it never stamps anything (escalations clear on resolve, notices on delivery)."""
    lines: list = []
    escalations = store.list_open_escalations()
    if escalations:
        lines.append("Waiting on your call:")
        lines.extend(f"• {e['open_question']}" for e in escalations)
    notices = store.list_queued_notices()
    if notices:
        lines.append("Updates:" if lines else "Updates for you:")
        lines.extend(f"• {n['text']}" for n in notices)
    if not lines:
        return "You're all caught up — nothing waiting."
    return "\n".join(lines)


def render_settings_card(store) -> str:
    """The `/selly` card: current state with values, closing with the free-text invitation. The
    settings-surface plan appends real settings lines; the frame is what ships here."""
    escalations, notices = _needs_me_counts(store)
    ch = store.get_channel()
    bound = "connected" if ch["chat_id"] is not None else "not connected"
    paused = "paused" if store.is_paused() else "active"
    lines = [
        "Here's where things stand:",
        f"• Agent: {paused}",
        f"• Telegram: {bound}",
        f"• Waiting on you: {escalations} decision(s), {notices} update(s)",
        "",
        "Tell me in plain words what you'd like to change.",
    ]
    return "\n".join(lines)
