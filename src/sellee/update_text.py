"""How a tell's words are made fit to show the seller: the buyer's and Sellee's messages quoted
without the contact details in them and cut at a sentence, titles clipped to a line, and amounts
written the way every other seller-facing price is.

Pure, for the same reasons as the composer that uses it (`sellee.updates`): it computes only on
the text it is handed.
"""

from __future__ import annotations

import re
import unicodedata

from sellee.engines import hosts

# Quotes and titles are clipped so a line stays a line on a phone.
QUOTE_MAX_CHARS = 80
TITLE_MAX_CHARS = 48
# A first sentence shorter than this is an opener ("Hi!", "Hello there.") rather than what the
# buyer asked, so the quote runs on into the next sentence.
QUOTE_MIN_CHARS = 12
ELLIPSIS = "…"


# --- money ---------------------------------------------------------------------------------------

# Currencies shown with a sign; any other reads as "60 SGD", the way every other seller-facing
# price does.
_CURRENCY_SIGNS = {"USD": "$"}


# A currency is shown only as the three-letter code the store keeps; anything else is left off
# rather than let a stray string run the line long.
_CURRENCY_CODE_RE = re.compile(r"[A-Z]{3}")


def money(value: float, currency: object) -> str:
    """An amount as the seller reads it: "$60" in dollars, "60 SGD" in any other currency, the
    bare number when the currency is not a three-letter code."""
    number = f"{value:.0f}" if value == int(value) else f"{value:.2f}"
    code = str(currency or "").strip().upper()
    if not _CURRENCY_CODE_RE.fullmatch(code):
        code = ""
    sign = _CURRENCY_SIGNS.get(code)
    if sign:
        return f"{sign}{number}"
    return f"{number} {code}" if code else number


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


def quote(text: str) -> str:
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
        return clip_words(first, QUOTE_MAX_CHARS)
    if len(first) < len(clean):
        return first.rstrip(_SENTENCE_CUT_PUNCTUATION) + ELLIPSIS
    return first


def clip_title(title: str) -> str:
    clean = " ".join(title.split())
    return clean if len(clean) <= TITLE_MAX_CHARS else clip_words(clean, TITLE_MAX_CHARS)


def clip_words(text: str, limit: int) -> str:
    """`text` cut to at most `limit` characters including the ellipsis, between words when there
    is a space to cut at."""
    room = text[: limit - len(ELLIPSIS) + 1]
    head = room.rsplit(" ", 1)[0] if " " in room else room[:-1]
    return head.rstrip(_CUT_PUNCTUATION) + ELLIPSIS
