"""What may not leave in a reply to a craigslist buyer.

One rule, and it is not about privacy.

**Contact details are fine.** An earlier draft of this module refused an outbound reply containing
an email address, a phone number or the seller's name, on the grounds that craigslist's relay
anonymises the address and nothing else — measured 2026-09-10, a buyer's message arrived from
`4cd598…@reply.craigslist.org` carrying the display name `jerry neo`, so anything in the body
reaches the other side verbatim.

That measurement is still true; the conclusion drawn from it was wrong. Exchanging a first name, a
phone number and a meeting spot **is** how a craigslist sale is arranged — it is what both sides
expect, and craigslist's own posting form invites a phone number. A lint that refuses those refuses
the transaction, and an agent that answers "I can't share that" to *when and where* is worse than
useless to a seller. Normal disclosure for a craigslist reply is allowed.

Two consequences worth being explicit about, since they were previously treated as defects:

  * **The seller's display name reaching the buyer is fine.** Gmail sends as the signed-in account
    and the DOM transport cannot change the name on an outgoing message. That is no longer a gap to
    disclose at connect — it is a person's name on a message to someone buying their desk lamp.
  * **This module no longer knows anything about identity.** It does not need to.

**What is still refused: a checkout link.** The reason is deliverability and trust, not secrecy:

  * craigslist's own safety guidance tells buyers never to pay through a link a seller sends, so a
    delivered link teaches the buyer to distrust us — correctly;
  * link-bearing relay mail is widely reported as silently dropped, and this transport has no way
    to tell a dropped message from an unanswered one.

So the close goes out as a short alphanumeric code the buyer types at `carousell.ai/redeem-checkout`
instead. This check is the boundary that makes that enforceable rather than advisory: the checkout
tool hands back a live URL, and "the skill says not to" is one model mistake away from a payment
link in a craigslist email.

Note what is *not* here, and deliberately not: the guard that keeps the agent from reading anything
in the mailbox except craigslist's mail. That is inbound, it lives in `relay.is_relay_sender` and
`gmail.MESSAGE_LIST_JS`, and it is untouched by any of the above.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REASON_CHECKOUT_LINK = "checkout_link"

_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


@dataclass(frozen=True)
class Refusal:
    """Why a body may not be sent, in terms the seller can act on.

    `reason` is the stable code a caller branches on; `detail` is for the seller's notice.
    """

    reason: str
    detail: str


def _host_of(url: str) -> str:
    if "//" not in url:
        return ""
    rest = url.split("//", 1)[1]
    return rest.split("/")[0].split("?")[0].lower()


def check(
    text: object,
    *,
    link_hostile: bool = True,
    checkout_hosts: tuple = (),
) -> Refusal | None:
    """The first reason this body may not be sent, or `None` if it may.

    `link_hostile` is a property of the *transport*, not of craigslist: any channel that cannot
    carry a link — mail through a relay that drops them, SMS, a phone call — wants the code instead.
    Passed in rather than assumed so the same check serves the next one.

    `checkout_hosts` is supplied by the caller because the rail's base URL is configuration; an
    empty tuple means nothing is refused, which is the honest behaviour when we do not know what a
    checkout link looks like.
    """
    said = str(text or "")
    if not said.strip() or not link_hostile:
        return None

    for found in _URL.finditer(said):
        host = _host_of(found.group(0))
        if not host:
            continue
        if any(host == want or host.endswith(f".{want}") for want in checkout_hosts):
            return Refusal(
                reason=REASON_CHECKOUT_LINK,
                detail=(
                    "the reply contains a checkout link. Craigslist's own advice tells buyers "
                    "never to pay through a link a seller sends, and link-bearing relay mail "
                    "is commonly dropped — the buyer gets a code to type instead"
                ),
            )

    return None
