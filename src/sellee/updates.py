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
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import TypedDict, TypeGuard, Union

from sellee.engines import hosts

TELL_BUYER_ANSWERED = "buyer_answered"
TELL_OFFER_DECLINED = "offer_declined"


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


# What a tell's facts may be. Neither shape has anywhere to put the floor.
TellFacts = Union[OfferDeclinedFacts, BuyerAnsweredFacts]


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
# Quotes and titles are clipped so a line stays a line on a phone.
QUOTE_MAX_CHARS = 80
TITLE_MAX_CHARS = 48
# A first sentence shorter than this is an opener ("Hi!", "Hello there.") rather than what the
# buyer asked, so the quote runs on into the next sentence.
QUOTE_MIN_CHARS = 12
# Past this many offers on one listing, their amounts are given as a range.
MAX_AMOUNTS_LISTED = 3
# The smallest single message among the channels a seller can bind (Discord's; Telegram allows
# 4096). An update is never split: the caps above keep it far inside this, and a text that somehow
# outgrows it drops whole listing lines rather than being sent as two messages.
UPDATE_MAX_CHARS = 2000
# The buttons on an update that asks for a decision: lower the minimum the offers fell under, or
# leave it where it is.
DECISION_OPTIONS = ("Lower my minimum", "Hold")

_EMOJI = {TELL_OFFER_DECLINED: "💰", TELL_BUYER_ANSWERED: "🗣"}
# The kinds whose line asks the seller for a decision.
_DECISION_KINDS = frozenset({TELL_OFFER_DECLINED})
# The text facts each kind's line reads. Each must be text or absent, or the tell cannot be read.
_TEXT_FACTS = {
    TELL_OFFER_DECLINED: ("currency", "item_title"),
    TELL_BUYER_ANSWERED: ("buyer_said", "we_said", "item_title"),
}
_CONTINUATION = "\n   "
_ELLIPSIS = "…"


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
    text, a yes-or-no for the first buyer, a finite number for an offer."""
    text_facts = _TEXT_FACTS.get(kind)
    if text_facts is None or not isinstance(facts, dict):
        return False
    if not all(isinstance(facts.get(name), (str, type(None))) for name in text_facts):
        return False
    if kind == TELL_BUYER_ANSWERED:
        return isinstance(facts.get("first_on_item"), (bool, type(None)))
    amount = facts.get("amount")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return False
    return math.isfinite(amount)


@dataclass(frozen=True)
class _Group:
    """The tells on one listing, which become one line."""

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
    def is_decision(self) -> bool:
        return any(t["kind"] in _DECISION_KINDS for t in self.tells)


@dataclass(frozen=True)
class _Piece:
    """One line of an update (a listing's line may wrap onto a continuation), the tells it tells
    and whether it asks the seller for a decision. Headings and blank lines tell nothing."""

    text: str
    ids: tuple[int, ...] = ()
    asks: bool = False


def compose_update(
    selection: TellSelection, context: UpdateContext, *, now: float
) -> Update | None:
    """The update carrying every tell `selection` kept, or None if it kept none.

    A few fresh tells read as one line per listing, naming everything told about it, the listings
    that need the seller first. Tells on many listings, or any held past `HELD_AFTER_SECONDS`,
    read as "While you were away:" with a section for what needs the seller and one for what was
    done for them."""
    tells = tuple(sorted(selection.tellable, key=lambda t: t["id"]))
    if not tells:
        return None
    groups = _groups(tells)
    status = _Status(
        talking=frozenset(context["talking_thread_ids"]),
        waiting=frozenset(context["waiting_thread_ids"]),
    )
    is_held = len(groups) >= HELD_MIN_LISTINGS or any(
        now - t["created_ts"] > HELD_AFTER_SECONDS for t in tells
    )
    pieces = _held_pieces(groups, status) if is_held else _short_pieces(groups, status)
    kept = _within_one_message(pieces)
    return Update(
        text=_joined(kept),
        options=DECISION_OPTIONS if any(p.asks for p in kept) else (),
        carried_ids=tuple(sorted(i for p in kept for i in p.ids)),
    )


@dataclass(frozen=True)
class _Status:
    """Where each answered thread's conversation stands, as `UpdateContext` reads it."""

    talking: frozenset[str]
    waiting: frozenset[str]


def _groups(tells: Iterable[TellableRecord]) -> tuple[_Group, ...]:
    by_item: dict[str, list[TellableRecord]] = {}
    for tell in tells:
        by_item.setdefault(tell["item_id"], []).append(tell)
    return tuple(_Group(item_id=item_id, tells=tuple(group)) for item_id, group in by_item.items())


def _listing_piece(group: _Group, status: _Status) -> _Piece:
    return _Piece(_line(group, status), ids=group.ids, asks=group.is_decision)


def _short_pieces(groups: Sequence[_Group], status: _Status) -> tuple[_Piece, ...]:
    """Listings that need the seller first, then the most recently active."""
    ordered = sorted(groups, key=lambda g: (not g.is_decision, -g.newest_ts))
    return tuple(_listing_piece(g, status) for g in ordered)


def _held_pieces(groups: Sequence[_Group], status: _Status) -> tuple[_Piece, ...]:
    sections = (
        _section("Needs you:", [g for g in groups if g.is_decision], status),
        _section("Done for you:", [g for g in groups if not g.is_decision], status),
    )
    body = tuple(piece for s in sections if s for piece in (_Piece(""), *s))
    return (_Piece("While you were away:"), *body)


def _section(heading: str, groups: Sequence[_Group], status: _Status) -> tuple[_Piece, ...]:
    if not groups:
        return ()
    ordered = sorted(groups, key=lambda g: -g.newest_ts)
    shown = ordered[:MAX_LINES_PER_SECTION]
    hidden = ordered[MAX_LINES_PER_SECTION:]
    # What was done for the seller is told by the count; a decision is not, since the seller can
    # only make it knowing which listing and which offers, so a hidden one stays untold and comes
    # back named in the next update.
    counted = tuple(i for g in hidden if not g.is_decision for i in g.ids)
    more = _Piece(f"+{len(hidden)} more", ids=counted)
    return (
        _Piece(heading),
        *(_listing_piece(g, status) for g in shown),
        *((more,) if hidden else ()),
    )


def _line(group: _Group, status: _Status) -> str:
    """One listing's line: its offers, then its answered buyers as "Also answered…"."""
    latest = max(group.tells, key=lambda t: (t["created_ts"], t["id"]))
    title = _clip_title(latest["facts"].get("item_title") or "") or "a listing"
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
    marker = _Piece(_ELLIPSIS)
    room = UPDATE_MAX_CHARS - len("\n" + _ELLIPSIS)
    fits = 0
    while fits < len(pieces) and len(_joined(pieces[: fits + 1])) <= room:
        fits += 1
    telling = [i for i, p in enumerate(pieces[:fits]) if p.ids]
    if telling:
        return (*pieces[: telling[-1] + 1], marker)
    first = next(i for i, p in enumerate(pieces) if p.ids)
    lead = tuple(pieces[:first])
    line_room = room - len(_joined(lead)) - (1 if lead else 0)
    clipped = replace(pieces[first], text=_clip_words(pieces[first].text, line_room))
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
    exchange = _exchange(_quote(facts.get("buyer_said") or ""), _quote(facts.get("we_said") or ""))
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


# --- money ---------------------------------------------------------------------------------------

# Currencies shown with a sign; any other reads as "60 SGD", the way every other seller-facing
# price does.
_CURRENCY_SIGNS = {"USD": "$"}


# A currency is shown only as the three-letter code the store keeps; anything else is left off
# rather than let a stray string run the line long.
_CURRENCY_CODE_RE = re.compile(r"[A-Z]{3}")


def _amount(facts: TellFacts) -> float:
    value = facts.get("amount")
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0.0
    return float(value)


def _money(facts: TellFacts) -> str:
    value = _amount(facts)
    number = f"{value:.0f}" if value == int(value) else f"{value:.2f}"
    currency = str(facts.get("currency") or "").strip().upper()
    if not _CURRENCY_CODE_RE.fullmatch(currency):
        currency = ""
    sign = _CURRENCY_SIGNS.get(currency)
    if sign:
        return f"{sign}{number}"
    return f"{number} {currency}" if currency else number


# --- quoting -------------------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# An email spaced out around its "@" or dots ("bob @ gmail . com"). A dot with a space after it
# but not before ends a sentence, so it does not count; and the last part must be a real-looking
# domain ending, so "meet @ 5 . see you" stays as written.
# A bracketed dot or "at", as written to slip past a filter: "[.]", "(dot)", "[@]", "(at)".
_BRACKETED_DOT = r"\s*[\[(]\s*(?:\.|dot)\s*[\])]\s*"
_BRACKETED_AT = r"\s*[\[(]\s*(?:@|at)\s*[\])]\s*"
_SPACED_DOT = rf"(?:\.|\s+\.\s+|{_BRACKETED_DOT})"
_SPACED_EMAIL_RE = re.compile(
    rf"[\w.+-]+\s*@\s*[\w-]+(?:{_SPACED_DOT}[\w-]+)*{_SPACED_DOT}(?P<tld>[a-z]{{2,}})\b",
    re.IGNORECASE,
)
# An email spelled out to slip past a filter: "bob at gmail dot com", "bob [at] gmail [.] com".
# The last part must be a real-looking domain ending, so "I'm at work dot…" stays as written.
_SPELLED_DOT = rf"(?:\s+dot\s+|{_BRACKETED_DOT})"
_SPELLED_EMAIL_RE = re.compile(
    rf"[\w.+-]+(?:\s+at\s+|{_BRACKETED_AT})[\w-]+"
    rf"(?:{_SPELLED_DOT}[\w-]+)*{_SPELLED_DOT}(?P<tld>[a-z]{{2,}})\b",
    re.IGNORECASE,
)
# A social or payment handle: "@bob_k", "venmo me @bobby".
_HANDLE_RE = re.compile(r"(?<![\w@])@\w{2,}")
# A payment or social app's name, as a buyer writes it before their id there.
_APP = (
    r"(?P<app>venmo|paypal|cash\s?app|zelle|paylah|paynow|instagram|insta|ig|telegram|"
    r"snapchat|snap|wechat|kik|discord|signal|line\s?id)"
)
# A plain id written after an app's name and a colon: "venmo: bobk", "line id: bobby99".
_APP_ID_RE = re.compile(rf"\b{_APP}\s*[:=]\s*[\w.\-]{{2,}}", re.IGNORECASE)
# An id written after an app's name and "is": "insta is bob_k99". Plain words follow "is" too
# ("venmo is fine"), so only a token that reads as an id, with a digit or an underscore in it, is
# taken for one.
_APP_ID_IS_RE = re.compile(
    rf"(?P<lead>\b{_APP}(?:\s+(?:id|handle|username))?\s+is\s+)(?=[\w.\-]*[\d_])[\w.\-]{{2,}}",
    re.IGNORECASE,
)
# A link with any scheme, not just the web's: "tg://resolve?domain=bobk", "weixin://dl/chat?bobk".
# A chat app opens these from a tap, so they are a contact as much as a web link is. Trailing
# punctuation is left out, so the sentence the link ended keeps its full stop.
_SCHEME_LINK_RE = re.compile(r"\b[a-z][a-z0-9+.\-]*://\S*[^\s.,;:!?)\]}>\"']", re.IGNORECASE)
# A run of digits and digit words ("six five zero 2 4 8 …"): a phone number spelled out so a
# filter on digits misses it. Masked only when it has a spelled digit and is long enough to dial.
_DIGIT_TOKEN = r"\b(?:\d+|zero|oh|one|two|three|four|five|six|seven|eight|nine)\b"
_DIGIT_TOKEN_RE = re.compile(_DIGIT_TOKEN, re.IGNORECASE)
_SPELLED_PHONE_RE = re.compile(rf"\+?{_DIGIT_TOKEN}(?:[\s,.\-–]+{_DIGIT_TOKEN})+", re.IGNORECASE)
_PHONE_MIN_DIGITS = 7
# Seven or more digits, optionally led by "+" or an opening bracket, with up to three separators
# between digits — "(650) 248-0749", "+65 9123 - 4567" — so a phone number however it is spaced.
# A run led by a currency sign is a price, and an ISO date is a date; neither is masked.
_PHONE_RE = re.compile(
    r"(?<![\w.$€£¥])(?!\d{4}-\d\d-\d\d(?!\d))\+?\(?\d(?:[\s().\-–]{0,3}\d){6,}(?![\w])"
)
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")
_CUT_PUNCTUATION = " .,;:!?-"
# A sentence cut short keeps its question mark: what was asked is the point of the quote.
_SENTENCE_CUT_PUNCTUATION = " .,;:!-"
# A straight double quote that opens a phrase: at the start, or after a space or bracket.
_OPENING_QUOTE_RE = re.compile(r'(?:^|(?<=[\s(\[]))"')
# The arrow the update writes between the buyer's words and ours. Inside a quote it becomes a
# hyphen, so a buyer cannot write a line that reads as an exchange that never happened.
_ARROWS = str.maketrans({"→": "-", "⟶": "-"})
# The ideographic full stop NFKC leaves alone, which reads as the dot in "bob@gmail。com". Only one
# between two Latin letters or digits is read so; one ending a Chinese sentence stays as written.
_LATIN_FULL_STOP_RE = re.compile(r"(?<=[A-Za-z0-9])。(?=[A-Za-z0-9])")
_ZERO_WIDTH_JOINER = "\u200d"
# What may stand before a joiner inside an emoji: a symbol, a skin tone, or the variation
# selector that asks for the emoji form of the symbol before it.
_EMOJI_PARTS = frozenset({"So", "Sk", "Mn"})


def _plausible_email(match: re.Match[str]) -> str:
    return "[email]" if match.group("tld").lower() in hosts.PLAUSIBLE_TLDS else match.group(0)


def _spelled_phone(match: re.Match[str]) -> str:
    tokens = _DIGIT_TOKEN_RE.findall(match.group(0))
    digits = sum(len(t) if t.isdigit() else 1 for t in tokens)
    is_spelled = any(not t.isdigit() for t in tokens)
    return "[phone]" if is_spelled and digits >= _PHONE_MIN_DIGITS else match.group(0)


def _without_contacts(text: str) -> str:
    """The buyer's words with emails (written out, spaced or spelled), handles, links and phone
    numbers (in digits or words) replaced by a plain marker: the seller is told something was there
    without the update becoming a way to pass it on. Links go before phone numbers, so a link with
    a number in it ("wa.me/…") is one link rather than a link around a phone, and a link with a
    scheme goes first of all, so whatever is inside it is one link too."""
    text = _SCHEME_LINK_RE.sub("[link]", text)
    text = _EMAIL_RE.sub("[email]", text)
    text = _SPACED_EMAIL_RE.sub(_plausible_email, text)
    text = _SPELLED_EMAIL_RE.sub(_plausible_email, text)
    text = _HANDLE_RE.sub("[handle]", text)
    text = _APP_ID_RE.sub(lambda m: f"{m.group('app')}: [handle]", text)
    text = _APP_ID_IS_RE.sub(r"\g<lead>[handle]", text)
    for link in sorted(hosts.extract_links(text), key=len, reverse=True):
        text = text.replace(link, "[link]")
    text = _SPELLED_PHONE_RE.sub(_spelled_phone, text)
    return _PHONE_RE.sub("[phone]", text)


def _neutral(text: str) -> str:
    """`text` with its straight double quotes made typographic and the exchange arrow a hyphen, so
    a quote cannot close early and write the rest of the line itself."""
    return _OPENING_QUOTE_RE.sub("“", text).replace('"', "”").translate(_ARROWS)


def _opening(text: str) -> str:
    """The first sentence, run on through the next while what is kept is shorter than
    `QUOTE_MIN_CHARS`."""
    for end in _SENTENCE_END_RE.finditer(text):
        if end.end() >= QUOTE_MIN_CHARS:
            return text[: end.end()]
    return text


def _is_emoji_joiner(text: str, at: int) -> bool:
    """Whether the zero-width joiner at `at` joins two symbols, the way an emoji like 👨‍👩‍👧 is
    built. No contact pattern reads a symbol, so a joiner there hides nothing."""
    if not 0 < at < len(text) - 1:
        return False
    before, after = text[at - 1], text[at + 1]
    return unicodedata.category(before) in _EMOJI_PARTS and unicodedata.category(after) == "So"


def _visible(text: str) -> str:
    """`text` without the characters that render as nothing, whitespace aside: zero-width spaces
    and joiners, the word joiner, the byte-order mark, soft hyphens, direction marks and the rest
    of Unicode's control and format characters. One inside an email or a number would split it
    out of every contact pattern while the seller still read it whole."""
    return "".join(
        ch
        for at, ch in enumerate(text)
        if ch.isspace()
        or unicodedata.category(ch)[0] != "C"
        or (ch == _ZERO_WIDTH_JOINER and _is_emoji_joiner(text, at))
    )


def _quote(text: str) -> str:
    """The opening sentence, contact details removed, within `QUOTE_MAX_CHARS`. Invisible
    characters are dropped and lookalikes folded first ("＠" reads as "@", "。" as "."), so a
    contact cannot hide behind them. Anything left out is marked with an ellipsis; a cut inside a
    sentence falls between words."""
    shown = _LATIN_FULL_STOP_RE.sub(".", unicodedata.normalize("NFKC", _visible(text)))
    folded = " ".join(shown.split())
    clean = _neutral(_without_contacts(folded))
    if not clean:
        return ""
    first = _opening(clean)
    if len(first) > QUOTE_MAX_CHARS:
        return _clip_words(first, QUOTE_MAX_CHARS)
    if len(first) < len(clean):
        return first.rstrip(_SENTENCE_CUT_PUNCTUATION) + _ELLIPSIS
    return first


def _clip_title(title: str) -> str:
    clean = " ".join(title.split())
    return clean if len(clean) <= TITLE_MAX_CHARS else _clip_words(clean, TITLE_MAX_CHARS)


def _clip_words(text: str, limit: int) -> str:
    """`text` cut to at most `limit` characters including the ellipsis, between words when there
    is a space to cut at."""
    room = text[: limit - len(_ELLIPSIS) + 1]
    head = room.rsplit(" ", 1)[0] if " " in room else room[:-1]
    return head.rstrip(_CUT_PUNCTUATION) + _ELLIPSIS
