"""Getting the buyer off craigslist's relay before it expires under them.

**The problem this solves, measured rather than argued.** Craigslist mints a fresh relay address on
every view of a posting — five distinct ones were observed for a single posting on 2026-09-10, two
of them from consecutive page reloads seconds apart (see `relay.py`). A buyer's conversation runs on
whichever address existed when they wrote, that address expires, and **nothing recovers it**:
re-reading the posting yields a new `sale.` address, which is what a *buyer* writes to in order to
reach the *seller*. Sending there mails the seller their own reply.

So a relay conversation has a shelf life, and when it runs out the buyer is unreachable. Worse, it
fails silently — a reply to a retired address was accepted by Gmail, appeared in Sent, produced no
bounce of any kind, and never arrived.

**The fix is to stop depending on the relay.** The reply carries an address of the seller's own and
invites the buyer to forward the thread there. From that point the conversation is ordinary email:
no rotation, no expiry, and normal bounce semantics if something goes wrong. This is also just what
craigslist sellers do — "email me directly at …" is unremarkable, and disclosing it is allowed
(see `outbound.py`).

**It is the seller's own address, plain — and an earlier version got this wrong.** That version
derived a `+cl` subaddress, on the reasoning that the transport may only read craigslist's mail and
a plain address is indistinguishable from the rest of the mailbox. The reasoning was sound and the
conclusion was still wrong: **the tag has to actually deliver**, and a seller whose provider does
not route `+` addressing gets an address that silently receives nothing — so buyers are invited to
write somewhere no one is listening, which is worse than not inviting them at all. Reported by the
seller whose address it was.

The scope comes from **content** instead, which needs nothing of the address. A forwarded craigslist
thread carries craigslist's own footer, and that phrase is what the view searches for. Measured on a
live mailbox: `"Original craigslist post"` matched four messages — two buyer messages and two
replies to them — and nothing else at all. So the scope stays narrow without asking the seller's
mail provider to support anything.

**Two honest limits.**

  * **The first reply still has to survive the relay.** If the address has already expired, the
    invitation never arrives either. This turns an every-message race into a one-message race — a
    large improvement, not immunity — so the reply lane still has to be prompt.
  * **It is the buyer's choice.** Nothing forces a forward. A buyer who ignores it stays on the
    relay and stays exposed to the expiry, which is why the line is worded as an easy next step
    rather than a condition of being helped.
"""

from __future__ import annotations

import re

# An ordinary address. A `+tag` is allowed — a seller who has one and knows it works may use it —
# but it is not required, because requiring one hands an address that does not deliver to any
# seller whose provider ignores subaddressing.
_ADDRESS = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


class HandoffError(ValueError):
    """The configured handoff address cannot be used, with the reason the seller needs."""


def parse(said: object) -> str:
    """The handoff address, case-folded, or a refusal saying what is wrong with it.

    An ordinary address, and deliberately so. The previous version **required** a `+tag` for
    scoping reasons and had to be reverted: an address that does not deliver is worse than a wide
    one, because a buyer invited to write there is a buyer nobody hears from and nobody knows they
    have lost. Scoping is a content question now (see the module docstring), so the address only has
    to be real.
    """
    value = str(said or "").strip().lower()
    if not value:
        return ""
    if " " in value or value.count("@") != 1:
        raise HandoffError(f"{said!r} is not an email address")
    if not _ADDRESS.match(value):
        raise HandoffError(f"{said!r} is not a usable email address")
    return value


# The invitation, as one sentence appended to a reply.
#
# Worded three ways deliberately:
#
#   * **a reason the buyer benefits from**, not ours. "So we don't lose the thread" is true for both
#     sides and does not ask them to care about craigslist's plumbing.
#   * **forward, not reply.** A forward carries craigslist's original message with it, and that
#     message contains the posting permalink — which is the only non-rotating thing in a relay
#     conversation and the join back to the item (`relay.posting_url`). A fresh email from the buyer
#     carries nothing we can attach to a listing.
#   * **no link.** Link-bearing relay mail is commonly dropped, and this line has to survive the one
#     delivery we cannot retry.
HANDOFF_LINE = (
    "One thing — craigslist's forwarding address for this ad stops working after a while, so if "
    "you forward this email to {address} we won't lose the thread."
)


def handoff_line(address: object) -> str:
    """The invitation to include in a reply, or `""` when there is no handoff address.

    Empty rather than raising, because a missing handoff address is a configuration gap and must not
    stop a buyer being answered. The reply goes without it and stays subject to the expiry.
    """
    value = str(address or "").strip()
    if not value:
        return ""
    return HANDOFF_LINE.format(address=value)


def with_handoff(body: object, address: object, *, already_sent: bool = False) -> str:
    """A reply body with the invitation appended, once per conversation.

    `already_sent` is the caller's record that this conversation has had the invitation. Repeating
    it every message reads as a bot and buries the answer the buyer actually asked for, so it goes
    out with the first reply and then stops.
    """
    said = str(body or "").rstrip()
    line = "" if already_sent else handoff_line(address)
    if not line:
        return said
    if not said:
        return line
    return f"{said}\n\n{line}"
