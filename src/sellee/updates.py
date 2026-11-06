"""Activity updates: one message to the seller about what Sellee did on their listings that they
have not been told, composed by code from the `tells` ledger.

Pure, like an engine. The caller reads the untold tells and the two facts about the present the
words depend on (which listings are still live, which buyers wrote after our last answer) and hands
them over with `now`; this module never touches the store, the clock or the network, so the same
rows always compose the same message. Nothing here can carry the floor: no shape it reads has a
place for one, and "under your minimum" is as close as the words get.

Two steps, so the caller can retire what will never be told even when nothing is left to send:
`select_tellable` sets aside tells on listings no longer live, tells too old to be news and tells
it cannot read, and `compose_update` renders what that selection kept, or returns None when there
is nothing to say. The composer takes only a selection, so nothing reaches the words without
passing those rules.

`compose_weekly` is the line for a seller nothing else reached all week: what is live, and whether
any buyer wrote.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import TypedDict, TypeGuard, Union

from sellee.update_text import ELLIPSIS, clip_title, clip_words, money, quote

TELL_BUYER_ANSWERED = "buyer_answered"
TELL_OFFER_DECLINED = "offer_declined"
TELL_ASK_WAITING = "ask_waiting"
TELL_LISTING_QUIET = "listing_quiet"
TELL_DEAL_CHECKIN = "deal_checkin"


class OfferDeclinedFacts(TypedDict):
    """An offer under the seller's minimum that was not taken — the buyer's number, never the
    minimum it fell under."""

    buyer_handle: str
    amount: float
    currency: str
    item_title: str


class BuyerAnsweredFacts(TypedDict):
    """A buyer we answered: their last words, ours, and whether they are the item's first buyer —
    no other buyer on it had been answered before them.

    `buyer_said` and `we_said` are the messages as sent, cut only to a bounded length: a buyer's
    words can carry links, emails and phone numbers. They are not fit to quote to the seller until
    the contact details are stripped and the text is clipped to a sentence."""

    buyer_handle: str
    buyer_said: str
    we_said: str
    first_on_item: bool
    item_title: str


class AskWaitingFacts(TypedDict):
    """A buyer left waiting on the seller's answer to an escalation: when it was asked and the
    buyer's newest words, as sent. The escalation's own question is not here: it is Sellee's to
    the seller, and could name the minimum."""

    buyer_handle: str
    buyer_said: str
    asked_ts: float
    item_title: str


class ListingQuietFacts(TypedDict):
    """A live listing no buyer has written about, and since when it has been live."""

    live_since_ts: float
    item_title: str


class DealCheckinFacts(TypedDict):
    """A sale the seller said they would close themselves, still neither sold nor released."""

    buyer_handle: str
    item_title: str


# What a tell's facts may be. No shape has anywhere to put the floor.
TellFacts = Union[
    OfferDeclinedFacts, BuyerAnsweredFacts, AskWaitingFacts, ListingQuietFacts, DealCheckinFacts
]


class TellRecord(TypedDict):
    """One untold `tells` row as the ledger holds it. `facts` is None when the stored facts are not
    a record at all. `decision` is the ledger's mark that the tell asks the seller for something;
    the composer goes by `kind`, which says the same for every kind it knows."""

    id: int
    item_id: str | None
    thread_id: str | None
    kind: str
    decision: bool
    facts: TellFacts | None
    created_ts: float


class TellableRecord(TypedDict):
    """A tell worth telling now: on a live listing, recent enough to be news, of a kind the
    composer knows, with every fact it reads checked."""

    id: int
    item_id: str
    thread_id: str | None
    kind: str
    facts: TellFacts
    created_ts: float


class UpdateContext(TypedDict):
    """What the words depend on that the tells themselves do not say, read by the caller at the
    moment it composes: the listings still live; the threads where the buyer has written since our
    last answer and is owed the next one ("still talking"); and the threads still ours to carry
    where the next word is the buyer's ("waiting on them"). A thread in neither is held, escalated
    or over, and its line says neither."""

    live_item_ids: list[str]
    talking_thread_ids: list[str]
    waiting_thread_ids: list[str]


@dataclass(frozen=True)
class TellSelection:
    """Tells worth telling now, and the ids of those to retire untold."""

    tellable: tuple[TellableRecord, ...]
    retired_ids: tuple[int, ...]


@dataclass(frozen=True)
class Update:
    """One composed update: its text, the buttons it offers (none unless it asks the seller for a
    decision), and every tell the text accounts for, so the caller can stamp them told. A tell
    whose line did not fit is not among them and stays untold for the next update."""

    text: str
    options: tuple[str, ...]
    carried_ids: tuple[int, ...]


# A tell this old was missed, not delayed; telling it now would report stale news as fresh.
STALE_AFTER_SECONDS = 48 * 3600
# Past this, an update reports what happened while the seller was away rather than just now.
HELD_AFTER_SECONDS = 6 * 3600
# This many listings in one update read better sorted into sections than as a run of lines.
HELD_MIN_LISTINGS = 3
# A section shows this many listing lines, then counts the rest.
MAX_LINES_PER_SECTION = 3
# Past this many offers on one listing, their amounts are given as a range.
MAX_AMOUNTS_LISTED = 3
# The smallest single message among the channels a seller can bind (Discord's; Telegram allows
# 4096). An update is never split: the caps above keep it far inside this, and a text that somehow
# outgrows it drops whole listing lines rather than being sent as two messages.
UPDATE_MAX_CHARS = 2000
# The buttons on an update that asks for a decision: lower the minimum the offers fell under, or
# leave it where it is.
DECISION_OPTIONS = ("Lower my minimum", "Hold")
# The buttons on a check-in about a sale the seller is closing themselves.
CHECKIN_OPTIONS = ("✅ Sold", "💔 Fell through", "⏳ Still on it")
# A buyer's handle is a name on a line, not a quote.
HANDLE_MAX_CHARS = 32
# Past this long a wait reads in days rather than hours.
WAIT_IN_DAYS_AFTER_SECONDS = 48 * 3600
# How many live listings the weekly line names before it counts the rest.
WEEKLY_MAX_TITLES = 3

_EMOJI = {
    TELL_OFFER_DECLINED: "💰",
    TELL_BUYER_ANSWERED: "🗣",
    TELL_ASK_WAITING: "⏳",
    TELL_LISTING_QUIET: "💤",
    TELL_DEAL_CHECKIN: "🤝",
}
# The kinds whose line needs the seller: told first, and never told only by a count.
_NEEDS_YOU_KINDS = frozenset({TELL_OFFER_DECLINED, TELL_ASK_WAITING, TELL_DEAL_CHECKIN})
# The kinds told one line per tell rather than gathered into their listing's activity line.
_OWN_LINE_KINDS = frozenset({TELL_ASK_WAITING, TELL_LISTING_QUIET, TELL_DEAL_CHECKIN})
# The buttons each asking kind puts on an update. A tap sends back only its label, so one update
# carries one kind's buttons, and for a check-in one sale's (see `_one_question`).
_KIND_OPTIONS = {TELL_OFFER_DECLINED: DECISION_OPTIONS, TELL_DEAL_CHECKIN: CHECKIN_OPTIONS}
# The text facts each kind's line reads. Each must be text or absent, or the tell cannot be read.
_TEXT_FACTS = {
    TELL_OFFER_DECLINED: ("currency", "item_title"),
    TELL_BUYER_ANSWERED: ("buyer_said", "we_said", "item_title"),
    TELL_ASK_WAITING: ("buyer_handle", "buyer_said", "item_title"),
    TELL_LISTING_QUIET: ("item_title",),
    TELL_DEAL_CHECKIN: ("buyer_handle", "item_title"),
}
# The numbers each kind's line reads. Each must be a finite number.
_NUMBER_FACTS = {
    TELL_OFFER_DECLINED: ("amount",),
    TELL_ASK_WAITING: ("asked_ts",),
    TELL_LISTING_QUIET: ("live_since_ts",),
}
_CONTINUATION = "\n   "
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 24 * _SECONDS_PER_HOUR


def select_tellable(
    tells: Sequence[TellRecord], context: UpdateContext, *, now: float
) -> TellSelection:
    """Split `tells` into those to tell and those to retire: a tell on a listing that is no longer
    live (or names none) is not news, neither is one untold for longer than the stale limit, and
    one this composer cannot read (a kind it does not know, facts that are not a record of that
    kind) would otherwise come back untold on every pass."""
    live = frozenset(context["live_item_ids"])
    tellable = tuple(filter(None, (_tellable(t, live, now) for t in tells)))
    kept = {t["id"] for t in tellable}
    retired = tuple(sorted(t["id"] for t in tells if t["id"] not in kept))
    return TellSelection(tellable=tellable, retired_ids=retired)


def _tellable(tell: TellRecord, live: frozenset[str], now: float) -> TellableRecord | None:
    item_id = tell["item_id"]
    facts = tell["facts"]
    if item_id is None or item_id not in live:
        return None
    if now - tell["created_ts"] > STALE_AFTER_SECONDS or not _is_readable(facts, tell["kind"]):
        return None
    return {
        "id": tell["id"],
        "item_id": item_id,
        "thread_id": tell["thread_id"],
        "kind": tell["kind"],
        "facts": facts,
        "created_ts": tell["created_ts"],
    }


def _is_readable(facts: object, kind: str) -> TypeGuard[TellFacts]:
    """Whether `facts` is a record of `kind` in every field its line reads: text where it reads
    text, a yes-or-no for the first buyer, a finite number for an amount or a time."""
    text_facts = _TEXT_FACTS.get(kind)
    if text_facts is None or not isinstance(facts, dict):
        return False
    if not all(isinstance(facts.get(name), (str, type(None))) for name in text_facts):
        return False
    if not all(_is_number(facts.get(name)) for name in _NUMBER_FACTS.get(kind, ())):
        return False
    return kind != TELL_BUYER_ANSWERED or isinstance(facts.get("first_on_item"), (bool, type(None)))


def _is_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return math.isfinite(value)


@dataclass(frozen=True)
class _Group:
    """The tells that become one line: what was done on one listing, or one tell of a kind told
    on a line of its own."""

    item_id: str
    tells: tuple[TellableRecord, ...]

    def of_kind(self, kind: str) -> tuple[TellableRecord, ...]:
        return tuple(t for t in self.tells if t["kind"] == kind)

    @property
    def ids(self) -> tuple[int, ...]:
        return tuple(t["id"] for t in self.tells)

    @property
    def newest_ts(self) -> float:
        return max(t["created_ts"] for t in self.tells)

    @property
    def needs_you(self) -> bool:
        return any(t["kind"] in _NEEDS_YOU_KINDS for t in self.tells)

    @property
    def is_quiet(self) -> bool:
        return any(t["kind"] == TELL_LISTING_QUIET for t in self.tells)

    @property
    def options(self) -> tuple[str, ...]:
        return next(
            (_KIND_OPTIONS[t["kind"]] for t in self.tells if t["kind"] in _KIND_OPTIONS), ()
        )


@dataclass(frozen=True)
class _Piece:
    """One line of an update (a listing's line may wrap onto a continuation), the tells it tells
    and the buttons it needs, if it asks the seller something they can tap an answer to. Headings
    and blank lines tell nothing."""

    text: str
    ids: tuple[int, ...] = ()
    options: tuple[str, ...] = ()


def compose_update(
    selection: TellSelection, context: UpdateContext, *, now: float
) -> Update | None:
    """The update carrying every tell `selection` kept, or None if it kept none.

    A few fresh tells read as one line per listing, naming everything told about it, the listings
    that need the seller first. Tells on many listings, or any held past `HELD_AFTER_SECONDS`,
    read as "While you were away:" with a section for what needs the seller, one for what was
    done for them and one for listings nobody has written about."""
    tells = _one_question(tuple(sorted(selection.tellable, key=lambda t: t["id"])))
    if not tells:
        return None
    groups = _groups(tells)
    status = _Status(
        talking=frozenset(context["talking_thread_ids"]),
        waiting=frozenset(context["waiting_thread_ids"]),
        now=now,
    )
    is_held = len(groups) >= HELD_MIN_LISTINGS or any(
        now - t["created_ts"] > HELD_AFTER_SECONDS for t in tells
    )
    pieces = _held_pieces(groups, status) if is_held else _short_pieces(groups, status)
    kept = _within_one_message(pieces)
    return Update(
        text=_joined(kept),
        options=next((p.options for p in kept if p.options), ()),
        carried_ids=tuple(sorted(i for p in kept for i in p.ids)),
    )


def _one_question(tells: tuple[TellableRecord, ...]) -> tuple[TellableRecord, ...]:
    """`tells` less the check-ins this update cannot ask about. A tap sends back only its label, so
    an update's buttons answer one question: offers under the minimum take them when there are
    any, and otherwise the oldest check-in does, so "Sold" names one sale. A check-in left out
    stays untold and is asked in the next update."""
    checkins = [t for t in tells if t["kind"] == TELL_DEAL_CHECKIN]
    if not checkins:
        return tells
    has_offers = any(t["kind"] == TELL_OFFER_DECLINED for t in tells)
    asked = None if has_offers else min(checkins, key=lambda t: (t["created_ts"], t["id"]))["id"]
    return tuple(t for t in tells if t["kind"] != TELL_DEAL_CHECKIN or t["id"] == asked)


@dataclass(frozen=True)
class _Status:
    """Where each answered thread's conversation stands, as `UpdateContext` reads it, and the
    moment the update is composed at, which a wait or a listing's age is counted to."""

    talking: frozenset[str]
    waiting: frozenset[str]
    now: float


def _groups(tells: Iterable[TellableRecord]) -> tuple[_Group, ...]:
    by_line: dict[tuple[str, int], list[TellableRecord]] = {}
    for tell in tells:
        own_line = tell["id"] if tell["kind"] in _OWN_LINE_KINDS else 0
        by_line.setdefault((tell["item_id"], own_line), []).append(tell)
    return tuple(
        _Group(item_id=item_id, tells=tuple(group)) for (item_id, _), group in by_line.items()
    )


def _listing_piece(group: _Group, status: _Status) -> _Piece:
    return _Piece(_line(group, status), ids=group.ids, options=group.options)


def _short_pieces(groups: Sequence[_Group], status: _Status) -> tuple[_Piece, ...]:
    """Lines that need the seller first and quiet listings last, each the most recent first."""
    ordered = sorted(groups, key=lambda g: (not g.needs_you, g.is_quiet, -g.newest_ts))
    return tuple(_listing_piece(g, status) for g in ordered)


def _held_pieces(groups: Sequence[_Group], status: _Status) -> tuple[_Piece, ...]:
    done = [g for g in groups if not g.needs_you and not g.is_quiet]
    sections = (
        _section("Needs you:", [g for g in groups if g.needs_you], status),
        _section("Done for you:", done, status),
        _section("Quiet listings:", [g for g in groups if g.is_quiet], status),
    )
    body = tuple(piece for s in sections if s for piece in (_Piece(""), *s))
    return (_Piece("While you were away:"), *body)


def _section(heading: str, groups: Sequence[_Group], status: _Status) -> tuple[_Piece, ...]:
    if not groups:
        return ()
    ordered = sorted(groups, key=lambda g: -g.newest_ts)
    shown = ordered[:MAX_LINES_PER_SECTION]
    hidden = ordered[MAX_LINES_PER_SECTION:]
    # What was done for the seller is told by the count; what needs them is not, since they can
    # only answer it knowing which listing and which buyer, so a hidden one stays untold and comes
    # back named in the next update.
    counted = tuple(i for g in hidden if not g.needs_you for i in g.ids)
    more = _Piece(f"+{len(hidden)} more", ids=counted)
    return (
        _Piece(heading),
        *(_listing_piece(g, status) for g in shown),
        *((more,) if hidden else ()),
    )


def _line(group: _Group, status: _Status) -> str:
    """One listing's line: its offers, then its answered buyers as "Also answered…"; or the line
    of a tell told on its own."""
    latest = max(group.tells, key=lambda t: (t["created_ts"], t["id"]))
    title = clip_title(latest["facts"].get("item_title") or "") or "a listing"
    own_line = _OWN_LINES.get(latest["kind"])
    if own_line is not None:
        return f"{_EMOJI[latest['kind']]} {title} — {own_line(latest['facts'], status.now)}"
    offers = group.of_kind(TELL_OFFER_DECLINED)
    answered = group.of_kind(TELL_BUYER_ANSWERED)
    if not offers:
        return f"{_EMOJI[TELL_BUYER_ANSWERED]} {title} — {_answered_body(answered, status)}"
    body = _offers_body(offers)
    if answered:
        body = f"{body} Also {_answered_body(answered, status)}"
    return f"{_EMOJI[TELL_OFFER_DECLINED]} {title} — {body}"


def _joined(pieces: Sequence[_Piece]) -> str:
    return "\n".join(p.text for p in pieces)


def _within_one_message(pieces: Sequence[_Piece]) -> tuple[_Piece, ...]:
    """`pieces` as they are when they fit one message. Otherwise the whole lines that fit with
    room to mark the cut, so each listing is told in full or left for the next update; only when
    not even the first listing's line fits is that line clipped, so the update still says
    something."""
    if len(_joined(pieces)) <= UPDATE_MAX_CHARS:
        return tuple(pieces)
    marker = _Piece(ELLIPSIS)
    room = UPDATE_MAX_CHARS - len("\n" + ELLIPSIS)
    fits = 0
    while fits < len(pieces) and len(_joined(pieces[: fits + 1])) <= room:
        fits += 1
    telling = [i for i, p in enumerate(pieces[:fits]) if p.ids]
    if telling:
        return (*pieces[: telling[-1] + 1], marker)
    first = next(i for i, p in enumerate(pieces) if p.ids)
    lead = tuple(pieces[:first])
    line_room = room - len(_joined(lead)) - (1 if lead else 0)
    clipped = replace(pieces[first], text=clip_words(pieces[first].text, line_room))
    is_more_untold = any(p.ids for p in pieces[first + 1 :])
    return (*lead, clipped, *((marker,) if is_more_untold else ()))


def _offers_body(tells: Sequence[TellableRecord]) -> str:
    amounts = [_money(t["facts"]) for t in tells]
    if len(amounts) == 1:
        return f"an offer of {amounts[0]}, under your minimum. I said no."
    if len(amounts) > MAX_AMOUNTS_LISTED:
        ranked = sorted(tells, key=lambda t: _amount(t["facts"]))
        listed = f"{_money(ranked[0]['facts'])} to {_money(ranked[-1]['facts'])}"
    else:
        listed = ", ".join(amounts)
    which = "both" if len(amounts) == 2 else "all"
    return f"{len(amounts)} offers ({listed}), {which} under your minimum. I said no."


def _answered_body(tells: Sequence[TellableRecord], status: _Status) -> str:
    threads = {t["thread_id"] or "" for t in tells}
    said = _where_it_stands(threads, status)
    if len(threads) > 1:
        return " ".join(filter(None, [f"answered {len(threads)} buyers.", said]))
    latest = max(tells, key=lambda t: (t["created_ts"], t["id"]))
    facts = latest["facts"]
    who = "its first buyer" if facts.get("first_on_item") else "a buyer"
    exchange = _exchange(quote(facts.get("buyer_said") or ""), quote(facts.get("we_said") or ""))
    if not exchange:
        return " ".join(filter(None, [f"answered {who}.", said]))
    head = f"answered {who} ({exchange})."
    return f"{head}{_CONTINUATION}{said}" if said else head


def _where_it_stands(threads: set[str], status: _Status) -> str:
    """ "Still talking." while any of these buyers is owed our next word, "Waiting on them." while
    the next word is theirs, and nothing when every thread is held, escalated or over: that is
    the seller's to know from elsewhere, and either phrase would be false."""
    if threads & status.talking:
        return "Still talking."
    if threads & status.waiting:
        return "Waiting on them."
    return ""


def _exchange(theirs: str, ours: str) -> str:
    if theirs and ours:
        return f'"{theirs}" → "{ours}"'
    if ours:
        return f'I said "{ours}"'
    if theirs:
        return f'they said "{theirs}"'
    return ""


def _waiting_body(facts: TellFacts, now: float) -> str:
    said = quote(str(facts.get("buyer_said") or ""))
    heard = f' ("{said}")' if said else ""
    waited = _how_long(now - _number(facts.get("asked_ts")))
    return (
        f"a buyer has waited {waited} for your answer{heard}."
        f"{_CONTINUATION}Reply here and I'll take it from there."
    )


def _quiet_body(facts: TellFacts, now: float) -> str:
    days = int((now - _number(facts.get("live_since_ts"))) // _SECONDS_PER_DAY)
    return (
        f"live {_count(days, 'day')} and no buyer has written yet."
        f"{_CONTINUATION}Want me to lower the price? Reply with the new one."
    )


def _checkin_body(facts: TellFacts, _now: float) -> str:
    who = _handle(facts.get("buyer_handle")) or "the buyer"
    return f"still on it with {who}? You said you'd close this sale yourself."


# The line of each kind told on its own, after its listing's title.
_OWN_LINES = {
    TELL_ASK_WAITING: _waiting_body,
    TELL_LISTING_QUIET: _quiet_body,
    TELL_DEAL_CHECKIN: _checkin_body,
}


def _how_long(seconds: float) -> str:
    if seconds >= WAIT_IN_DAYS_AFTER_SECONDS:
        return _count(int(seconds // _SECONDS_PER_DAY), "day")
    return _count(int(seconds // _SECONDS_PER_HOUR), "hour")


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _handle(raw: object) -> str:
    """A buyer's handle fit to name on a line: marketplace text, so contact details come out and
    it is clipped like any quote."""
    shown = quote(str(raw or ""))
    return shown if len(shown) <= HANDLE_MAX_CHARS else clip_words(shown, HANDLE_MAX_CHARS)


def compose_weekly(
    titles: Sequence[str], *, buyers: int, aged: Sequence[str], quiet: Sequence[str] = ()
) -> str | None:
    """The weekly line for a seller who has heard nothing else all week: which listings are live,
    and whether any buyer wrote. `titles` are the live listings, `buyers` how many buyers wrote
    about them in the week, `aged` the titles among them live a full week: the only ones it offers
    to lower when nobody wrote, since a listing up a few days has not had a quiet week. `quiet` are
    the listings no buyer has ever written about that the line also tells, named when other buyers
    did write so the count does not read as interest in them. None with nothing live."""
    if not titles:
        return None
    head = f"👋 Still on it: {_count(len(titles), 'listing')} live ({_named(titles)})."
    if buyers:
        wrote = f"{head} {_count(buyers, 'buyer')} wrote this week"
        if not quiet:
            return f"{wrote}."
        return f"{wrote}, none yet about {_named(quiet)}. {_offer_to_lower(quiet, quiet)}"
    quiet_week = f"{head} No buyers wrote this week."
    if not aged:
        return quiet_week
    return f"{quiet_week} {_offer_to_lower(aged, titles)}"


def _offer_to_lower(offered: Sequence[str], named: Sequence[str]) -> str:
    """The offer to lower one of `offered`, which are among the `named` listings the line has just
    listed: by pronoun when they are all of them, otherwise by name."""
    if len(offered) == len(named):
        if len(offered) == 1:
            return "Want me to lower it? Tell me the new price."
        return "Want me to lower one? Tell me which and the new price."
    if len(offered) == 1:
        return f"Want me to lower {_named(offered)}? Tell me the new price."
    return f"Want me to lower one of {_named(offered)}? Tell me which and the new price."


def _named(titles: Sequence[str]) -> str:
    """Up to `WEEKLY_MAX_TITLES` titles, each one clipped line in quotes so a comma in a title
    does not read as two listings, then a count of the rest."""
    shown = [_quoted_title(t) for t in titles[:WEEKLY_MAX_TITLES]]
    hidden = len(titles) - len(shown)
    return ", ".join(shown) + (f" and {hidden} more" if hidden else "")


def _quoted_title(title: str) -> str:
    clipped = clip_title(title)
    return f"“{clipped}”" if clipped else "a listing"


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and _is_number(value) else 0.0


def _amount(facts: TellFacts) -> float:
    return _number(facts.get("amount"))


def _money(facts: TellFacts) -> str:
    return money(_amount(facts), facts.get("currency"))
