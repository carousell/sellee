"""Craigslist's mail relay: which senders count, and what identifies a conversation.

Two jobs, both pure, and both load-bearing for a different reason.

**The sender guard.** No mail provider offers a per-sender read scope — the narrowest read grant any
of them has is the whole mailbox — so "only read craigslist mail" cannot be promised by a
credential. A scoped *view* (a seller-created label, or a search) is the first layer, so the DOM the
transport reads only ever holds relay mail; this is the second, applied to every row before anything
is parsed, so a view that is wrong or has since been changed cannot leak a message into the store.

The obvious implementation — `"craigslist.org" in address` — accepts
`rcc1@sale.craigslist.org.evil.com` and every other lookalike an attacker can register, so the
boundary here is a domain boundary.

**The thread key, and why it is not the address.** Craigslist uses two address families:

    <hex>@sale.craigslist.org     an address a buyer writes to, to reach the seller
    <hex>@reply.craigslist.org    the sender of a buyer's message as it reaches the seller

The plan assumed the second was per-conversation, so a thread could be keyed on it. An earlier
version of this docstring then concluded, from one measurement, that the hex was **per posting**.
Both were wrong, and the truth is stranger and more consequential.

**Measured on one live posting (id 7963439877) on 2026-09-10 — five distinct addresses:**

    4cd598c01d62398ba33718c2199ba1c0   the address a buyer's message arrived on
    65acaebd5a6532cfa2f291cd47e939c3   revealed to a logged-out viewer
    69d7b648f566356f9b1740368d905ef8   revealed to the seller's own signed-in session
    effa3e5a63223750ab7a013986bc3f92   \\ two consecutive reloads of the same page,
    913041a5e86f365fb6ac7393780ff341   /  seconds apart

**Craigslist mints a fresh relay address on every reveal.** It is not per posting, not per
conversation, and not per viewer — it is per *view*. That is an anti-scraping design, and it means
the address carries no identity of any kind.

Three consequences, and the middle one changes what this transport can promise:

  * **The thread key must not be the address** — the original conclusion, now on much firmer
    ground. Identity comes from the **mail provider's own thread id**, which is per-conversation by
    construction.
  * **A rotated address cannot be recovered by re-reading the posting.** An earlier design had
    `stale address -> re-read the posting -> resend`; a fresh read yields a brand-new address bound
    to nothing.

**And the relay has never been observed carrying a reply back at all.** This docstring previously
asserted that "a reply can only go to the `reply.` address the buyer's own message arrived from" —
stated as fact from a single inference, and not supported by anything measured. What was then
measured, on one live posting on 2026-09-10:

    buyer -> seller, <hex>@sale.craigslist.org       worked 3 of 3
    seller -> buyer, <hex>@reply.craigslist.org      accepted, no bounce, never arrived  (2x)
    seller -> buyer, <hex>@sale.craigslist.org       accepted, no bounce, never arrived  (1x)
    seller -> buyer, a HAND-TYPED reply, no code     accepted, no bounce, never arrived  (1x)
    seller -> buyer, a *stale* hex                   bounced 550 "get a current reply email
                                                     address"

The hand-typed control is what makes this a fact about craigslist rather than about this code, and
the stale-hex bounce is what rules out a blind catch-all: the relay has routing state and chose to
accept and discard the live sends. Craigslist's own documentation predicts none of it — their help
page says contact information "passes through unaltered" and that threads "continue for up to 4
months", their relay-error page lists no silent-drop case, and both relay domains resolve the same
MX (`mxia.craigslist.org`), so it is not a send-only domain either.

So `market_adapters.READ_ONLY_BUYERS` withdraws the promise: a craigslist buyer is **read** and
their words passed to the seller, and answering them is the seller's. Everything below still works
and is still used for the read; the send path is kept and refuses, because a transport that cannot
deliver must say so rather than accept.
  * **The hex cannot join a message to an item.** Craigslist puts the posting's own URL in the
    message body and that does not rotate, so `posting_url` is the join.

And the failure mode is silent. A reply sent to a retired address was accepted by Gmail ("Message
sent", present in Sent), produced **no bounce of any kind**, and never reached the buyer. So neither
a send confirmation nor the absence of a bounce is evidence that a buyer was answered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The market a relay conversation belongs to. Not the mailbox: `tools/reply.py` refuses a send
# unless the thread's market is one the seller connected, and nobody connects Gmail as a
# marketplace.
MARKET = "craigslist"

# Where a conversation's own address lives, and where a posting's does. Both are subdomains, and
# the distinction between them is the whole of the thread key.
CONVERSATION_DOMAIN = "reply.craigslist.org"
POSTING_DOMAIN = "sale.craigslist.org"

# Deliberately not `craigslist.org` itself. Craigslist sends plenty of mail from the bare domain
# that is not a buyer — a posting confirmation from `robot@craigslist.org`, for one — and folding
# those in as buyer messages would have the agent replying to craigslist's own robot.
RELAY_DOMAINS = (POSTING_DOMAIN, CONVERSATION_DOMAIN)

_ADDRESS = re.compile(r"[^<\s]+@[^>\s,;]+")


@dataclass(frozen=True)
class RelayAddresses:
    """The addresses a relay message carries, by what each one identifies.

    `conversation` is what a reply goes to and what a thread is keyed on. `posting` is which item
    the message is about. `reply_to` is the header the conversation address was taken from, kept so
    a wrong answer is traceable to the header it came from.
    """

    conversation: str | None
    posting: str | None
    reply_to: str | None


def _addresses(value: object) -> list:
    """Every address in a header value, bare and lowercased.

    Headers arrive rendered differently by every provider — `Name <addr>`, several addresses, odd
    whitespace — so the shape is not assumed; the addresses are extracted.
    """
    return [found.group(0).strip().lower() for found in _ADDRESS.finditer(str(value or ""))]


def _domain_of(address: str) -> str:
    _, _, domain = str(address or "").strip().lower().rpartition("@")
    return domain


def is_relay_sender(address: object) -> bool:
    """Whether this address is one of craigslist's relay addresses.

    Matched on the domain exactly, never as a substring: `sale.craigslist.org.evil.com` contains
    the relay domain and is not it, and an address whose *local part* mentions it is not it either.
    """
    for found in _addresses(address):
        if _domain_of(found) in RELAY_DOMAINS:
            return True
    return False


def is_in_scope(address: object, *, handoff: object = "") -> bool:
    """Whether this transport may read mail from this address.

    Two clauses, and the second is why this exists beside `is_relay_sender` rather than replacing
    it. `is_relay_sender` answers one question — *is this craigslist's relay* — and a lot depends on
    that answer meaning exactly that. This answers the broader one the mailbox read needs.

    **The handoff clause.** A relay address expires and cannot be recovered (see the module
    docstring), so a reply hands the buyer a durable address of the seller's own to continue on. The
    moment the buyer uses it they are `somebuyer@gmail.com` — not a craigslist sender — and a
    relay-only guard blocks the conversation the handoff just created.

    **Whole address, never a domain.** The handoff lives on the seller's own mail domain, so
    matching that domain would admit their entire mailbox and undo the point of scoping at all. A
    subaddress (`seller+cl@example.com`) is compared in full, case-folded, and nothing else on that
    domain is in scope.
    """
    wanted = str(handoff or "").strip().lower()
    for found in _addresses(address):
        if _domain_of(found) in RELAY_DOMAINS:
            return True
        if wanted and found == wanted:
            return True
    return False


def quotes_a_posting(body: object) -> bool:
    """Whether this message quotes a craigslist posting.

    The content half of the scope. A buyer who forwards a relay thread is writing from their own
    ordinary address, so no address test can admit them — but the forward carries craigslist's
    footer and the posting permalink, and that is checkable.

    Both are required: the footer phrase *and* a host-anchored posting URL. The phrase alone would
    admit a message that merely mentions craigslist, and the URL alone would admit a pasted link.
    """
    said = str(body or "")
    if FOOTER_MARKER not in said:
        return False
    return posting_url(said) is not None


def relay_addresses(headers: dict) -> RelayAddresses:
    """The conversation and posting addresses a message carries.

    `Reply-To` is preferred for the conversation, because that is the header craigslist puts there
    for the purpose — the address a reply must go back through. Failing that, `From`.
    """
    reply_to = None
    conversation = None
    for header in ("reply_to", "reply-to", "from"):
        for found in _addresses((headers or {}).get(header)):
            if _domain_of(found) == CONVERSATION_DOMAIN:
                conversation = found
                reply_to = reply_to or (found if header != "from" else None)
                break
        if conversation:
            break

    posting = None
    for header in ("to", "from", "reply_to", "reply-to", "cc"):
        for found in _addresses((headers or {}).get(header)):
            if _domain_of(found) == POSTING_DOMAIN:
                posting = found
                break
        if posting:
            break

    return RelayAddresses(conversation=conversation, posting=posting, reply_to=reply_to)


def thread_key(provider_thread_id: object) -> str:
    """The thread id for a relay conversation: `craigslist:<provider thread id>`.

    Market-prefixed because `store.create_thread` requires it, and prefixed with *craigslist*
    rather than the mailbox because that is the marketplace the buyer is on.

    The identity is the mail provider's own thread id, not the relay address. Craigslist's relay
    address is per posting rather than per conversation (see the module docstring — measured, not
    assumed), so keying on it would merge every buyer who wrote about one item into one thread.
    The provider groups a conversation for its own reasons, and that grouping is the only
    per-conversation fact a scoped mailbox read has.

    Refuses an empty id: a conversation nothing can identify must not silently become a thread
    that later collides with another.
    """
    said = str(provider_thread_id or "").strip()
    if not said:
        raise ValueError("a relay conversation needs a provider thread id to be a thread")
    if is_relay_sender(said):
        raise ValueError(
            f"{said!r} is a relay address, which identifies a posting rather than a conversation "
            "— every buyer on one posting shares it, so it cannot key a thread"
        )
    return f"{MARKET}:{said}"


# Craigslist's own words when a posting's relay address has rotated out from under a sender. The
# operative phrase, kept narrow so a different 550 is not swept into it.
_STALE_ADDRESS = re.compile(r"current reply email address|email_relay_error", re.IGNORECASE)
_OPTED_OUT = re.compile(r"opted out|unsubscrib|no longer accepting", re.IGNORECASE)

# A craigslist posting permalink, anchored on the host so a pasted lookalike is not mistaken for
# the posting the conversation is about.
_POSTING_URL = re.compile(
    r"https://(?:[a-z0-9-]+\.)?craigslist\.org/(?:view/)?[A-Za-z0-9/_-]*d/[^\s<>\"']+",
    re.IGNORECASE,
)

# What a bounce means for the conversation, which is not the same as what it means for the address.
BOUNCE_STALE = "stale_address"
BOUNCE_OPTED_OUT = "opted_out"
BOUNCE_UNKNOWN = "unknown"


def bounce_kind(text: object) -> str:
    """What a bounce says about the conversation.

    Three outcomes, and none of them is "retry":

      * `BOUNCE_OPTED_OUT` — the buyer asked not to receive mail. Ending the conversation is the
        only correct response, and retrying is mailing someone who opted out.
      * `BOUNCE_STALE` — the address has been retired. The conversation is **unreachable**, not
        recoverable: re-reading the posting yields a freshly-minted `sale.` address that routes to
        the seller, not to this buyer (see the module docstring — measured). So this ends the
        conversation too, but for a different reason and with a different thing to tell the seller:
        their buyer is waiting and cannot be replied to.
      * `BOUNCE_UNKNOWN` — neither. Reported once rather than guessed in either direction.

    The distinction is kept even though two of the three now close the conversation, because what
    the seller is told differs: "they opted out" versus "craigslist retired the address before we
    answered", and the second is our latency problem rather than the buyer's choice.
    """
    said = str(text or "")
    if not said:
        return BOUNCE_UNKNOWN
    # Opt-out first: a bounce can cite the help page while still being an opt-out, and the
    # narrower, more consequential reading wins.
    if _OPTED_OUT.search(said):
        return BOUNCE_OPTED_OUT
    if _STALE_ADDRESS.search(said):
        return BOUNCE_STALE
    return BOUNCE_UNKNOWN


def posting_url(body: object) -> str | None:
    """The posting a message is about, taken from its body.

    Craigslist includes the posting's own permalink in relay mail, and unlike the relay address it
    does not rotate — so it is the only thing in a message that can be matched against a listing
    URL we stored. Host-anchored, because a buyer can paste anything and a link crafted to look
    like the posting would otherwise join their message to the wrong item.
    """
    found = _POSTING_URL.search(str(body or ""))
    if not found:
        return None
    url = found.group(0).rstrip(".,);")
    host = url.split("/")[2].lower()
    if host != "craigslist.org" and not host.endswith(".craigslist.org"):
        return None
    return url


# Craigslist's own footer, appended to every relay message, captured verbatim from a live one on
# 2026-09-10. Three labelled blocks, each followed by a URL on its own line:
#
#     Original craigslist post:
#     https://www.craigslist.org/view/d/san-francisco-…/q2Dcuy4bHvytRxM1T27fC9
#     About craigslist mail:
#     https://www.craigslist.org/about/help/posting/features/contact-info/email/mail-relay
#     Please flag unwanted messages (spam, scam, other):
#     https://post.craigslist.org/mailflag?flagCode=34&smtpid=e6ffc83c…
#
# Matched by label rather than by URL, because the labels are craigslist's fixed strings while the
# URLs vary per posting and per message.
# The first of them, named on its own because it does two jobs: it is where a body is cut, and it
# is the phrase `gmail.search_view` searches for to keep a *forwarded* thread in scope. One
# constant, so the view and the parser cannot drift apart.
FOOTER_MARKER = "Original craigslist post"

BOILERPLATE_MARKERS = (
    f"{FOOTER_MARKER}:",
    "About craigslist mail:",
    "Please flag unwanted messages",
)


def buyer_text(body: object) -> str:
    """What the buyer actually wrote, with craigslist's footer removed.

    Every relay message carries a three-block footer of craigslist's own links. Left in, it becomes
    part of the buyer's turn: journaled as their words, compared by `reconcile` when aligning a
    tail against stored rows, and read by the model as though the buyer had pasted three URLs and
    asked to flag themselves for spam. It also puts two more craigslist links in front of a model
    that is told to navigate only URLs it was given.

    Cut at the *earliest* marker present, so a footer whose blocks are reordered still goes. If the
    buyer's own reply quotes an older footer below their new text, cutting at the first marker keeps
    exactly the new text — which is the right answer for a top-posting mail client.

    A message that is nothing but footer returns `""`. That is honest: the caller decides what an
    empty buyer turn means, and returning the footer instead would invent words the buyer never
    wrote.
    """
    said = str(body or "")
    if not said:
        return ""
    cuts = [found for found in (said.find(marker) for marker in BOILERPLATE_MARKERS) if found >= 0]
    if cuts:
        said = said[: min(cuts)]
    return said.strip()


def posting_hex(address: object) -> str | None:
    """Removed: a relay address's hex identifies nothing.

    This returned the local part as "the posting a relay address belongs to", on the strength of one
    measurement showing the `sale.` and `reply.` hexes matching for a posting. Five addresses were
    later observed for that same posting — two from consecutive reloads seconds apart — so
    craigslist mints a fresh address per *view*. The hex is not a posting id, a conversation id, or
    a viewer id.

    Kept as a raising stub rather than deleted so that a caller reintroduced from the old design
    fails loudly instead of joining a buyer's message to an arbitrary item. Use `posting_url`.
    """
    raise NotImplementedError(
        "a relay address's hex identifies nothing — craigslist mints a fresh address per view "
        "(five observed for one posting). Join a message to an item with posting_url() instead"
    )
