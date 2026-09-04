"""Provider-agnostic fast-path logic: the deterministic commands the daemon answers itself (no
LLM), their store effects, and the text renders.

This is the "what" — which token does what, and what the reply text is. The "how" (rendering the
control row into a provider's native widget, sending it) stays in the provider. `handle_fast_path`
returns the reply text plus a controls *spec* — a plain list of (label, token) buttons, or None —
so the core never builds a Telegram keyboard or a Slack block; the provider renders the spec.
"""

from __future__ import annotations

import time

from sellee import marketplaces, prompt_data, settings
from sellee.browser import markets as market_adapters
from sellee.browser import window
from sellee.store.browser import CONNECT_MODE_OPEN, CONNECT_MODE_PROBE

# The commands answered deterministically (exact first-word token). Everything else routes to the
# channel pass.
_FAST_PATH_COMMANDS = frozenset(
    {"/pause", "/resume", "/status", "/catchup", "/sellee", "/connect", "/watch"}
)

# Callback tokens the control row emits. Provider-neutral: a provider carries them in whatever its
# interactive widget uses (Telegram callback_data, Slack action_id). The settings surface reuses the
# same callback plumbing for its Approve/Cancel/Undo buttons (different tokens, routed to a door).
CB_PAUSE = "pause"
CB_RESUME = "resume"
CB_NEEDS_ME = "needsme"
# The first-listing CTA's "Skip for now" button (outbound.queue_welcome attaches it).
CB_SKIP_CTA = "skipcta"
# Marketplace sign-in, from the logged-out notice and the /connect picker. Both carry the market
# as the callback ref (`carousell:connectmkt`), the same ref:token shape the settings doors use.
# Open = "sign me in"; probe = "I've signed in, look again".
CB_CONNECT_MARKET = "connectmkt"
CB_CONNECT_PROBE = "connectchk"
# The two answers to the take-these-over ask. The ref carries the market, so a tap months later
# still says which list it meant.
CB_SURVEY_YES = "adoptyes"
CB_SURVEY_NO = "adoptno"
# Watch mode, from the control row — one token per direction, like pause/resume, so the button does
# what its label says whenever it lands.
#
# This was a single valueless token that flipped whatever was set at landing time. The argument for
# it was that a button carrying a value would apply a stale one from the scrollback; the argument
# against it is what a seller reported: they tapped "🌙 Work in background" on a card from an hour
# earlier, watch mode came ON, they tapped again, and it went off — so the button read as broken.
# A label that names an action has to perform that action. A stale idempotent tap is harmless and
# re-acks, which is the rule the marketplace buttons right below already follow.
CB_WATCH_ON = "watchon"
CB_WATCH_OFF = "watchoff"
# Connecting and disconnecting a marketplace, from the Connections block on the /sellee card. The
# market rides in the ref, not a toggle: a scrollback tap must name the market it flips.
CB_ADD_MARKET = "addmkt"
CB_REMOVE_MARKET = "rmmkt"
_FAST_PATH_CALLBACKS = frozenset(
    {
        CB_PAUSE,
        CB_RESUME,
        CB_NEEDS_ME,
        CB_SKIP_CTA,
        CB_CONNECT_MARKET,
        CB_CONNECT_PROBE,
        CB_SURVEY_YES,
        CB_SURVEY_NO,
        CB_WATCH_ON,
        CB_WATCH_OFF,
        CB_ADD_MARKET,
        CB_REMOVE_MARKET,
    }
)
_WATCH_VALUE_FOR_CALLBACK = {CB_WATCH_ON: True, CB_WATCH_OFF: False}

_CONNECT_MODE_FOR_CALLBACK = {
    CB_CONNECT_MARKET: CONNECT_MODE_OPEN,
    CB_CONNECT_PROBE: CONNECT_MODE_PROBE,
}

# Button labels, defined once because three surfaces attach them (the logged-out notice, and the
# lane's two retry notices) and a seller who sees the same door worded three ways cannot tell it
# is the same door. "on desktop" is the load-bearing half: this is tapped on a phone and acted on
# at a computer, and a bare "Sign in" reads like something the phone is about to do.
SIGN_IN_LABEL = "Sign in on desktop"
CHECK_AGAIN_LABEL = "Check again"
# The two answers to the ask. One yes: an agent that answers buyers on a listing it cannot relist
# has to explain that split in every conversation. The tools carry the finer answers.
SURVEY_YES_LABEL = "Yes, manage them"
SURVEY_NO_LABEL = "No thanks"
# The way back from a look that could not be served — rides CB_SURVEY_YES, whose handler already
# reopens a survey for a market with nothing left to decide.
LOOK_AGAIN_LABEL = "Take another look"
# The watch-mode toggle. Each label names what tapping *does*, not what is currently set — the card
# line right above it carries the state, and a button that named the state would read as a claim.
WATCH_ON_LABEL = "👀 Watch me work"
WATCH_OFF_LABEL = "🌙 Work in background"


def signin_controls(market: str) -> list:
    """The one-button control spec that opens `market` for sign-in."""
    return [(SIGN_IN_LABEL, f"{market}:{CB_CONNECT_MARKET}")]


def survey_controls(market: str) -> list:
    """The two-button spec the take-these-over ask carries. Here rather than beside the copy in
    `browser/survey.py`, so the tokens live with every other button token."""
    return [
        (SURVEY_YES_LABEL, f"{market}:{CB_SURVEY_YES}"),
        (SURVEY_NO_LABEL, f"{market}:{CB_SURVEY_NO}"),
    ]


def look_again_controls(market: str) -> list:
    """The one-button spec on the notice that a market's listings could not be read.

    Without it the market is a dead end: neither `due` nor `done`, so no lane picks it up again.
    """
    return [(LOOK_AGAIN_LABEL, f"{market}:{CB_SURVEY_YES}")]


def check_again_controls(market: str) -> list:
    """The one-button control spec that re-probes `market` without touching the window."""
    return [(CHECK_AGAIN_LABEL, f"{market}:{CB_CONNECT_PROBE}")]


# What the seller sees the moment they tap. Chrome cold-starts in seconds, so this promises a
# follow-up rather than a moment — the lane sends the real answer when it has one.
CONNECT_ACK = (
    "Opening {name} in my Chrome now — it takes a few seconds to come up. I'll message you the "
    "moment the sign-in page is there."
)
CONNECT_CHECK_ACK = "Checking whether you're signed in to {name} — one moment while I look."
CONNECT_PICK = "Which marketplace do you want to sign in to?"
CONNECT_NONE = (
    "You don't have any marketplaces switched on that I sign in to — /sellee to turn one on."
)
CONNECT_UNKNOWN = "I don't sell on {market}, so there's nothing for me to open."
CONNECT_DISCONNECTED = (
    "{name} isn't connected any more, so there's nothing for me to sign in to. Turn it back on "
    "from /sellee and I'll open it."
)
SURVEY_UNKNOWN = "I don't have a list of listings for that marketplace any more."

# Watch mode, both ways. The on side says where the window is for the same reason every other
# window notice does: this is read on a phone and the window is on a desktop. It promises the tab
# will follow rather than promising the window will jump, because following works everywhere and
# raising does not — and it names the exception (a read tick) so a quiet five minutes doesn't read
# as the toggle not having worked.
WATCH_ON_NOTICE = (
    "Watch mode on — my Chrome window{where} will come forward when I start something, and its tab "
    "follows whatever page I'm on. Reading your inboxes stays quiet in the background."
)
WATCH_OFF_NOTICE = "Watch mode off — I'll keep out of your way and work in the background."
# A tap on the state already set. These buttons sit in the scrollback forever, so this is ordinary
# rather than exceptional — the same re-ack the marketplace switches give, and the honest answer to
# a stale card. Saying "already" is what tells the seller nothing moved, so they do not tap again.
WATCH_ALREADY_ON = "Watch mode is already on — my Chrome comes forward when I start something."
WATCH_ALREADY_OFF = "Watch mode is already off — I'm working in the background."

# Connecting a marketplace. The reply names sign-in as the next step: nothing can be read until the
# seller is signed in at the desktop where my Chrome is.
CONNECT_MARKET_LABEL = "Connect {name}"
DISCONNECT_MARKET_LABEL = "Disconnect {name}"
MARKET_ADDED_NOTICE = (
    "{name} is on — I'm opening it in my Chrome now so you can sign in. That takes a few seconds; "
    "I'll message you the moment the sign-in page is there. After that I'll read your inbox and "
    "answer buyers for you."
)
MARKET_REMOVED_NOTICE = (
    "{name} is off — I've stopped reading it, answering buyers on it, and listing to it. Your "
    "sign-in stays put, so turning it back on takes one tap and no password."
)
MARKET_ALREADY_ON = "{name} is already on."
MARKET_ALREADY_OFF = "{name} is already off."
MARKET_CANT_CONNECT = "I can't work {name}, so there's nothing to turn on."

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


def handle_fast_path(store, bus, event: dict) -> tuple:
    """Apply a fast path and return (reply_text, controls_spec | None). Pause/resume flip the
    control flag here (the enforcement — gating passes and killing a running one — lives in the
    pause wiring); the reads render from the store. Assumes is_fast_path(event) is True.

    The bus is here for the one fast path that changes a *setting* rather than a control flag: the
    settings ledger publishes what it applied, and the window raise rides on that event.
    """
    token = event["text"] if event["kind"] == "command" else event["payload"]["choice"]
    if token == "/watch":
        # The command carries no argument, so it stays a flip — it is typed in the present moment,
        # which is the one case where "the opposite of now" is unambiguous.
        return _watch_set(store, bus, not settings.get(store, window.WATCH_SETTING))
    if token in _WATCH_VALUE_FOR_CALLBACK:
        return _watch_set(store, bus, _WATCH_VALUE_FOR_CALLBACK[token])
    if token in (CB_ADD_MARKET, CB_REMOVE_MARKET):
        return _market_button(store, bus, event["payload"].get("ref"), token)
    if token in (CB_SURVEY_YES, CB_SURVEY_NO):
        return _survey_button(store, event["payload"].get("ref"), token)
    if token in _CONNECT_MODE_FOR_CALLBACK:
        return _connect_button(
            store, event["payload"].get("ref"), _CONNECT_MODE_FOR_CALLBACK[token]
        )
    if token == "/connect":
        return _connect_command(store, bus)
    if token in ("/pause", CB_PAUSE):
        store.set_paused(True, source="channel")
        return "Paused — I won't act on anything until you resume.", _control_spec(store)
    if token in ("/resume", CB_RESUME):
        store.set_paused(False, source="channel")
        return "Resumed — I'm back on.", _control_spec(store)
    if token == "/status":
        return render_status(store), None
    if token == "/catchup":
        return render_catchup(store), None
    if token == CB_SKIP_CTA:
        # Idempotent: a stale tap months later just re-acks (buttons live forever in history).
        store.set_meta(META_FIRST_LISTING_SKIPPED, str(time.time()))
        return "No problem — whenever you're ready, just send a photo.", None
    # /sellee and the what-needs-me button both render the settings card + control row.
    return render_settings_card(store), _control_spec(store)


def _signin_markets(store) -> list:
    """The marketplaces `/connect` can offer: the ones the seller switched on that the agent has a
    browser adapter for.

    Every marketplace we could drive for this seller, connected or not — offering only what is
    already on would make the command useless for setting a new marketplace up. carousell.ai is
    excluded: it is reached with an API key, so there is no window to open.
    """
    return market_adapters.connectable_markets(store.seller_region())


def _connect_button(store, market, mode: str) -> tuple:
    """A tap on Sign in on desktop / Check again. The market rides in the callback ref, so this
    never has to guess which one they meant — even months later, from a button in the
    scrollback.

    Two ways a tap can be stale: an adapter withdrawn since (nothing to open ever) and a market
    disconnected since (nothing to open now) — only the first earns "I don't sell there".
    """
    if not market or market not in market_adapters.connectable_markets(store.seller_region()):
        # A stale button, for a market whose adapter has since been withdrawn.
        return CONNECT_UNKNOWN.format(market=market or "that marketplace"), None
    if market not in settings.connected_markets(store):
        return CONNECT_DISCONNECTED.format(name=marketplaces.display_name(market)), None
    return _request(store, market, mode)


def _market_button(store, bus, market, token: str) -> tuple:
    """A tap on Connect / Disconnect in the Connections block.

    Applied immediately rather than proposed: an authenticated tap on the seller's own card is the
    signal the approval gate waits for, so this goes through `set_now`, which still runs the parser
    and seller-state check and still records the prior value for Undo. A tap that changes nothing
    re-acks — these buttons live in the scrollback forever.
    """
    adding = token == CB_ADD_MARKET
    if not market:
        return SURVEY_UNKNOWN, None
    name = marketplaces.display_name(market)
    current = settings.connected_markets(store)
    if adding and market not in market_adapters.connectable_markets(store.seller_region()):
        return MARKET_CANT_CONNECT.format(name=name), _control_spec(store)
    if adding and market in current:
        return MARKET_ALREADY_ON.format(name=name), _control_spec(store)
    if not adding and market not in current:
        return MARKET_ALREADY_OFF.format(name=name), _control_spec(store)

    wanted = sorted(set(current) | {market}) if adding else [m for m in current if m != market]
    try:
        settings.set_now(
            store, bus, key="connected_markets", raw_value=wanted, decided_via="button"
        )
    except settings.SettingError as exc:
        # The registry or the seller-state check refused it; its message is written for the seller.
        return str(exc), _control_spec(store)
    if adding:
        # On and signed-in are one intent — until the seller signs in there is nothing to read. Just
        # the row: opening Chrome takes seconds and this is the provider's receive loop.
        store.request_market_connect(market, CONNECT_MODE_OPEN)
    template = MARKET_ADDED_NOTICE if adding else MARKET_REMOVED_NOTICE
    return template.format(name=name), _control_spec(store)


def _survey_button(store, market, token: str) -> tuple:
    """A tap on Yes, manage them / No thanks.

    The buttons stay on the message forever, so every tap is a question about *when* it was tapped.
    Zero rows moved can mean the ask went stale, or the seller already answered and the work is
    under way — a zero is disambiguated by what the market still holds, and only a market holding
    nothing at all is treated as stale. That case re-asks, which is the one deliberate way back
    through the ask-once guard.
    """
    # Imported here: the survey lane imports this module for its button tokens, so the reverse
    # would close the loop.
    from sellee.browser import markets as market_adapters
    from sellee.browser import survey
    from sellee.store.survey import LISTING_ACCEPTED, LISTING_ADOPTED

    if not market:
        return SURVEY_UNKNOWN, None
    decision = "decline" if token == CB_SURVEY_NO else "manage"
    moved = store.decide_discovered_listings(
        market, decision=decision, manage=None if decision == "decline" else "relist"
    )
    if moved:
        if decision == "decline":
            return survey.declined_text(market), None
        return survey.accepted_text(store, market, moved), None

    # Nothing moved. What is already here says which kind of nothing this is.
    rows = store.list_discovered_listings(market)
    in_flight = sum(1 for row in rows if row["status"] == LISTING_ACCEPTED)
    adopted = sum(1 for row in rows if row["status"] == LISTING_ADOPTED)
    if decision == "decline":
        # Decline reaches acceptances not yet adopted; zero means nothing was left to stop.
        if adopted and not in_flight:
            return survey.already_managing_text(market, adopted), None
        return survey.declined_text(market), None
    if in_flight or adopted:
        # Already said yes. Re-ack, but never reopen the survey: that deletes the accepted rows
        # their first tap created.
        return survey.accepted_text(store, market, in_flight or adopted), None
    if not market_adapters.can_survey(market, store.seller_region()):
        return SURVEY_UNKNOWN, None
    store.reopen_market_survey(market)
    return survey.stale_text(market), None


def _connect_command(store, bus) -> tuple:
    """`/connect`, which carries no argument — the providers normalize a command to its first word
    — so the market is resolved here. One candidate is unambiguous; several is a question, and
    asking it as buttons keeps the answer a tap rather than a spelling.

    An unconnected market is offered too; picking it connects it as well as opening it — one
    intent, carried by the add token.
    """
    markets = _signin_markets(store)
    if not markets:
        return CONNECT_NONE, None
    connected = set(settings.connected_markets(store))
    if len(markets) == 1:
        market = markets[0]
        if market in connected:
            return _request(store, market, CONNECT_MODE_OPEN)
        return _market_button(store, bus, market, CB_ADD_MARKET)
    controls = [
        (
            marketplaces.display_name(market),
            f"{market}:{CB_CONNECT_MARKET if market in connected else CB_ADD_MARKET}",
        )
        for market in markets
    ]
    return CONNECT_PICK, controls


def _request(store, market: str, mode: str) -> tuple:
    """Hand the market to the connect lane and tell the seller what to expect.

    Nothing here touches Chrome: this runs on the provider's receive loop, which is answering
    every other message in the chat, and opening a cold Chrome takes seconds to tens of seconds.
    The lane picks the row up within a tick and sends the real answer.
    """
    store.request_market_connect(market, mode)
    template = CONNECT_ACK if mode == CONNECT_MODE_OPEN else CONNECT_CHECK_ACK
    return template.format(name=marketplaces.display_name(market)), None


def _watch_set(store, bus, watching: bool) -> tuple:
    """Put watch mode where the seller asked for it: work in front of them, or out of their way.

    The tap *is* the consent — an authenticated surface, a deterministic parse, a deterministic
    apply — so this applies immediately rather than proposing, exactly as the shell door does. It
    goes through the settings ledger rather than writing a row of its own, which is what gives it
    one home with the card line, the CLI, and the model's vocabulary.

    Idempotent, and that is the point: the value comes from the button rather than from whatever
    happened to be set when the tap landed, so a card from months back still does what it says. A
    tap that changes nothing re-acks — the same answer `_market_button` gives, and for the same
    reason, since these buttons live in the scrollback forever. The reply carries the refreshed row,
    whose label is now the way back, which is why this door renders no separate Undo.
    """
    if settings.get(store, window.WATCH_SETTING) == watching:
        return (WATCH_ALREADY_ON if watching else WATCH_ALREADY_OFF), _control_spec(store)
    settings.set_now(
        store, bus, key=window.WATCH_SETTING, raw_value=watching, decided_via="button"
    )
    text = WATCH_ON_NOTICE.format(where=window.where()) if watching else WATCH_OFF_NOTICE
    return text, _control_spec(store)


def _control_spec(store) -> list:
    """The one control row as provider-neutral (label, token) buttons: a pause/resume toggle
    reflecting current state, a what-needs-me shortcut, the watch-mode toggle, and one connect or
    disconnect button per marketplace. The provider wraps it onto as many rows as it takes, so this
    stays a flat list."""
    toggle = ("▶️ Resume", CB_RESUME) if store.is_paused() else ("⏸ Pause", CB_PAUSE)
    watching = settings.get(store, window.WATCH_SETTING)
    watch = (WATCH_OFF_LABEL, CB_WATCH_OFF) if watching else (WATCH_ON_LABEL, CB_WATCH_ON)
    return [toggle, ("What needs me", CB_NEEDS_ME), watch] + _connection_controls(store)


def _connection_controls(store) -> list:
    """One button per marketplace we could work for this seller, each naming what tapping does.

    Built from `connectable_markets`, not from what is connected, so an off market is discoverable
    here.
    """
    connected = set(settings.connected_markets(store))
    controls = []
    for market in market_adapters.connectable_markets(store.seller_region()):
        name = marketplaces.display_name(market)
        if market in connected:
            controls.append(
                (DISCONNECT_MARKET_LABEL.format(name=name), f"{market}:{CB_REMOVE_MARKET}")
            )
        else:
            controls.append((CONNECT_MARKET_LABEL.format(name=name), f"{market}:{CB_ADD_MARKET}"))
    return controls


def _connections_lines(store) -> list:
    """The Connections block: every marketplace we could work, and whether it is on.

    Says nothing about being signed in — the login probe runs on the read lane, which owns that
    conversation.
    """
    markets = market_adapters.connectable_markets(store.seller_region())
    if not markets:
        return ["• Marketplaces: carousell.ai only — there are no others I can work yet"]
    connected = set(settings.connected_markets(store))
    lines = ["Marketplaces I can work for you:"]
    for market in markets:
        state = "on" if market in connected else "off"
        lines.append(f"• {marketplaces.display_name(market)} — {state}")
    return lines


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


def _held_line(store) -> str:
    """What the agent still owes the seller an answer for, or "" when it owes nothing.

    The missing half of both reads. Everything else here reports what waits on the *seller*; a
    message of theirs the agent has not answered waits the other way, and it was reported nowhere —
    so a seller whose tapped answer was queued behind a running pass saw the identical open question
    printed back at them, with no way to tell "never arrived" from "in hand, still working".

    Pause gets its own wording because the wait is a different shape: a paused agent claims the row
    into a pass its lane will never run, so nothing ends that wait except the seller resuming.
    """
    held = store.count_unsettled_inbox()
    if not held:
        return ""
    noun = "message" if held == 1 else "messages"
    if store.is_paused():
        them = "it" if held == 1 else "them"
        return f"I'm holding {held} {noun} you sent — resume me and I'll act on {them}."
    return f"I'm still working through {held} {noun} you sent."


def render_status(store) -> str:
    """A one-glance status line: paused?, the counts of what's waiting, and anything the agent is
    still holding of the seller's."""
    escalations, notices = _needs_me_counts(store)
    paused = "paused" if store.is_paused() else "running"
    waiting = escalations + notices
    if waiting:
        line = f"Status: {paused}. {escalations} decision(s) and {notices} update(s) waiting."
    else:
        line = f"Status: {paused}. Nothing waiting on you."
    held = _held_line(store)
    return f"{line} {held}" if held else line


def render_catchup(store) -> str:
    """The deep needs-me view: each open escalation's question, then queued updates. This is a
    render only — it never stamps anything (escalations clear on resolve, notices on delivery)."""
    lines: list = []
    escalations = store.list_open_escalations()
    if escalations:
        lines.append("Waiting on your call:")
        # One bullet per escalation: the question is buyer-derived (the reply pass composed it
        # while reading a stranger), and a newline in it would read as an extra escalation.
        lines.extend(f"• {prompt_data.one_line(e['open_question'])}" for e in escalations)
    # Directly under the questions, because that is the confusion it resolves: an escalation stays
    # open until something resolves it, so a seller who has already answered one sees their own
    # question printed back and reads it as their answer having gone nowhere.
    held = _held_line(store)
    if held:
        lines.append(held)
    notices = store.list_queued_notices()
    if notices:
        lines.append("Updates:" if lines else "Updates for you:")
        # Flattened for the same reason: this is a bulleted list, so one notice has to be one
        # bullet whatever it contains.
        lines.extend(f"• {prompt_data.one_line(n['text'])}" for n in notices)
    pending = settings.pending_view(store)
    if pending:
        lines.append("Changes awaiting your OK:")
        lines.extend(
            f"• {p['label']}: {p['current']} → {p['proposed']} (approve {p['change_id']})"
            for p in pending
        )
    # An ask that scrolled out of the chat is otherwise unreachable. Surfaced for the same reason
    # pending settings changes are.
    found = store.count_pending_discovered()
    if found:
        lines.append(
            f"Listings you already had: {found} waiting on whether I should manage them "
            "(just tell me yes or no)."
        )
    if not lines:
        return "You're all caught up — nothing waiting."
    return "\n".join(lines)


def render_settings_card(store) -> str:
    """The `/sellee` card: current state, the marketplaces and their switches, then the settings
    lines (changed-from-default plus the headline set), closing with the free-text invitation. A
    capped summary — discovery is by display, mutation by free text or the buttons this card
    carries; get_settings carries the tail."""
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
        "",
    ]
    lines.extend(_connections_lines(store))
    lines.append("")
    lines.extend(settings.card_lines(store))
    lines += ["", "Tell me in plain language what you'd like to change."]
    return "\n".join(lines)
