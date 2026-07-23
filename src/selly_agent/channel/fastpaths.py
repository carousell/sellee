"""Provider-agnostic fast-path logic: the deterministic commands the daemon answers itself (no
LLM), their store effects, and the text renders.

This is the "what" — which token does what, and what the reply text is. The "how" (rendering the
control row into a provider's native widget, sending it) stays in the provider. `handle_fast_path`
returns the reply text plus a controls *spec* — a plain list of (label, token) buttons, or None —
so the core never builds a Telegram keyboard or a Slack block; the provider renders the spec.
"""

from __future__ import annotations

# The commands answered deterministically (exact first-word token). Everything else routes to the
# channel pass.
_FAST_PATH_COMMANDS = frozenset({"/pause", "/resume", "/status", "/catchup", "/selly"})

# Callback tokens the control row emits. Provider-neutral: a provider carries them in whatever its
# interactive widget uses (Telegram callback_data, Slack action_id). The settings-surface plan
# reuses the same callback plumbing for its approve button (a different token, routed there).
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
    """Apply a fast path and return (reply_text, controls_spec | None). Pause/resume flip the
    control flag here (the enforcement — gating passes and killing a running one — lives in the
    pause wiring); the reads render from the store. Assumes is_fast_path(event) is True."""
    token = event["text"] if event["kind"] == "command" else event["payload"]["choice"]
    if token in ("/pause", CB_PAUSE):
        store.set_paused(True, source="telegram")
        return "Paused — I won't act on anything until you resume.", _control_spec(store)
    if token in ("/resume", CB_RESUME):
        store.set_paused(False, source="telegram")
        return "Resumed — I'm back on.", _control_spec(store)
    if token == "/status":
        return render_status(store), None
    if token == "/catchup":
        return render_catchup(store), None
    # /selly and the what-needs-me button both render the settings card + control row.
    return render_settings_card(store), _control_spec(store)


def _control_spec(store) -> list:
    """The one control row as provider-neutral (label, token) buttons: a pause/resume toggle
    reflecting current state, plus a what-needs-me shortcut. The provider renders it."""
    toggle = ("▶️ Resume", CB_RESUME) if store.is_paused() else ("⏸ Pause", CB_PAUSE)
    return [toggle, ("What needs me", CB_NEEDS_ME)]


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
