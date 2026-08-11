"""Provider-agnostic fast-path logic: the deterministic commands the daemon answers itself (no
LLM), their store effects, and the text renders.

This is the "what" — which token does what, and what the reply text is. The "how" (rendering the
control row into a provider's native widget, sending it) stays in the provider. `handle_fast_path`
returns the reply text plus a controls *spec* — a plain list of (label, token) buttons, or None —
so the core never builds a Telegram keyboard or a Slack block; the provider renders the spec.
"""

from __future__ import annotations

import time

from selly_agent import settings

# The commands answered deterministically (exact first-word token). Everything else routes to the
# channel pass.
_FAST_PATH_COMMANDS = frozenset({"/pause", "/resume", "/status", "/catchup", "/selly"})

# Callback tokens the control row emits. Provider-neutral: a provider carries them in whatever its
# interactive widget uses (Telegram callback_data, Slack action_id). The settings surface reuses the
# same callback plumbing for its Approve/Cancel/Undo buttons (different tokens, routed to a door).
CB_PAUSE = "pause"
CB_RESUME = "resume"
CB_NEEDS_ME = "needsme"
# The first-listing CTA's "Skip for now" button (outbound.queue_welcome attaches it).
CB_SKIP_CTA = "skipcta"
_FAST_PATH_CALLBACKS = frozenset({CB_PAUSE, CB_RESUME, CB_NEEDS_ME, CB_SKIP_CTA})

# The one meta row this surface writes: when the seller tapped Skip on the first-listing CTA. An
# explicit seller answer is genuine, underivable state; the nudge lane reads it to stay quiet.
META_FIRST_LISTING_SKIPPED = "first_listing_cta_skipped_ts"

_DECISION_FOR_CALLBACK = {
    settings.CB_APPROVE: settings.DECIDE_APPROVE,
    settings.CB_CANCEL: settings.DECIDE_CANCEL,
    settings.CB_UNDO: settings.DECIDE_UNDO,
}
_DECISION_FOR_VERB = {
    settings.TEXT_APPROVE: settings.DECIDE_APPROVE,
    settings.TEXT_CANCEL: settings.DECIDE_CANCEL,
    settings.TEXT_UNDO: settings.DECIDE_UNDO,
}


def _settings_text_decision(text: str | None) -> tuple | None:
    """Parse an exact-token settings door — a bare '<verb> <chg_id>' text, nothing more. Returns
    (decision, change_id) or None. The strict two-word / chg_ shape keeps a conversational
    'approve the buyer's offer' from ever tripping the deterministic apply."""
    if not text:
        return None
    parts = text.split()
    if len(parts) != 2:
        return None
    verb, change_id = parts[0].lower(), parts[1]
    if verb not in settings.TEXT_VERBS or not change_id.startswith("chg_"):
        return None
    return _DECISION_FOR_VERB[verb], change_id


def is_settings_door(event: dict) -> bool:
    """True if `event` is a settings decision door — an Approve/Cancel/Undo button, or an exact
    '<verb> <chg_id>' text token."""
    if event["kind"] == "action":
        return (event.get("payload") or {}).get("choice") in settings.CALLBACK_CHOICES
    if event["kind"] == "text":
        return _settings_text_decision(event.get("text")) is not None
    return False


def is_fast_path(event: dict) -> bool:
    """True if `event` (a normalized inbox row's kind/text/payload) is one the daemon answers
    itself. A command matches on its exact first-word token; an action on its callback choice; a
    settings door on its button token or exact text token."""
    if event["kind"] == "command":
        return event["text"] in _FAST_PATH_COMMANDS
    if event["kind"] == "action":
        choice = (event.get("payload") or {}).get("choice")
        return choice in _FAST_PATH_CALLBACKS or choice in settings.CALLBACK_CHOICES
    return is_settings_door(event)


def handle_settings_door(store, bus, event: dict) -> tuple:
    """Apply a settings decision door and return (reply_text, controls_spec | None). The parse and
    the apply are both deterministic (settings.decide); no LLM sits between the authenticated
    tap/token and the state change. A channel decision replies synchronously and carries its own
    Undo button (settings.decide returns the controls), so it never also queues an echo notice —
    that is what keeps the seller from seeing the confirmation twice. Assumes is_settings_door."""
    if event["kind"] == "action":
        decision = _DECISION_FOR_CALLBACK[event["payload"]["choice"]]
        change_id = (event.get("payload") or {}).get("ref")
        decided_via = "button"
    else:
        decision, change_id = _settings_text_decision(event["text"])
        decided_via = "token"
    if not change_id:
        return "That action was missing its change id — ask me again.", None
    result = settings.decide(
        store, bus, change_id=change_id, decision=decision, decided_via=decided_via
    )
    return result["message"], result.get("controls")


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
    if token == CB_SKIP_CTA:
        # Idempotent: a stale tap months later just re-acks (buttons live forever in history).
        store.set_meta(META_FIRST_LISTING_SKIPPED, str(time.time()))
        return "No problem — whenever you're ready, just send a photo.", None
    # /selly and the what-needs-me button both render the settings card + control row.
    return render_settings_card(store), _control_spec(store)


def _control_spec(store) -> list:
    """The one control row as provider-neutral (label, token) buttons: a pause/resume toggle
    reflecting current state, plus a what-needs-me shortcut. The provider renders it."""
    toggle = ("▶️ Resume", CB_RESUME) if store.is_paused() else ("⏸ Pause", CB_PAUSE)
    return [toggle, ("What needs me", CB_NEEDS_ME)]


def _needs_me_counts(store) -> tuple:
    return store.count_open_escalations(), store.count_queued_notices()


def _listings_line(store) -> str:
    """One honest line on the card: what is live, what is on its way, what has sold — or the
    first-listing invitation when nothing exists yet. "Live" means a verified listing URL and no
    settled sale (the negotiation ledger is the one honest answer to "still for sale")."""
    items = store.list_items()
    if not items:
        return "• Listings: none yet — send a photo to start your first"
    sold_ids = store.sold_item_ids()
    sold = sum(1 for i in items if i["id"] in sold_ids)
    live = sum(1 for i in items if i["listing_urls"] and i["id"] not in sold_ids)
    in_progress = len(items) - live - sold
    counts = ((live, "live"), (in_progress, "in progress"), (sold, "sold"))
    return "• Listings: " + ", ".join(f"{n} {label}" for n, label in counts if n)


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
    pending = settings.pending_view(store)
    if pending:
        lines.append("Changes awaiting your OK:")
        lines.extend(
            f"• {p['label']}: {p['current']} → {p['proposed']} (approve {p['change_id']})"
            for p in pending
        )
    if not lines:
        return "You're all caught up — nothing waiting."
    return "\n".join(lines)


def render_settings_card(store) -> str:
    """The `/selly` card: current state, then the settings lines (changed-from-default plus the
    headline set), closing with the free-text invitation. A capped summary — discovery is by
    display, mutation by free text; get_settings carries the tail."""
    escalations, notices = _needs_me_counts(store)
    ch = store.get_channel()
    bound = "connected" if ch["chat_id"] is not None else "not connected"
    paused = "paused" if store.is_paused() else "active"
    lines = [
        "Here's where things stand:",
        f"• Agent: {paused}",
        f"• Telegram: {bound}",
        f"• Waiting on you: {escalations} decision(s), {notices} update(s)",
        _listings_line(store),
    ]
    lines.extend(settings.card_lines(store))
    lines += ["", "Tell me in plain language what you'd like to change."]
    return "\n".join(lines)
