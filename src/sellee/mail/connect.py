"""Connecting the mailbox a marketplace's buyers reach the seller through.

Craigslist has no on-site inbox, so answering its buyers means reading the seller's mailbox — which
makes Craigslist the first market needing **two sign-ins**. This is the second one.

Three things happen here, in an order that is itself load-bearing:

1. **Probe.** Read back whether the seller is signed in, and *which provider they signed into*. A
   webmail this transport has never been measured against is refused by name, because a wrong read
   of a mailbox is not a cosmetic failure — it is buyer mail journaled from a DOM we do not
   understand, or a mailbox that reports itself permanently unreadable.
2. **Confirm the view.** Navigate the scoped search in a **fresh tab** and read it. Only then is the
   view recorded. A view written before it is read flips the seller from an actionable message
   ("sign in to your mailbox") to an unactionable one ("I can't read your mailbox").
3. **Derive the handoff, and confirm the view queries it.** The address is the mailbox's own plus a
   tag, so there is nothing to type; what is checked is that the scoped view *asks about it
   coherently*.

Step 3 looks like ceremony and is not. Craigslist mints a fresh relay address on every view of a
posting and an expired one is unrecoverable, so a conversation that stays on the relay eventually
goes silent with no bounce and no error. The handoff address is the only durable way back to a buyer
— and since a payment link may only be sent once a conversation has moved onto it, it is also the
only way a Craigslist sale closes.

**What is checked, and what is left to prove itself.** An earlier design sent a synthetic message to
the address at connect time to prove delivery. That was dropped for two reasons: a synthetic test
can pass while a real forward fails, and this Gmail exposes no compose control in either view the
transport uses (`[gh="cm"]` absent from both the inbox and a scoped search, measured). So the check
here is that the `deliveredto:` clause is well-formed and answered — which is the failure that
actually happened, a literal `+` reading as a space and turning the whole view into an empty
mailbox with a buyer's thread in it. **Delivery proves itself**: `off_relay_ts` on a thread can only
be set by a real message arriving at the handoff, so the gate that depends on delivery rests on
evidence that cannot be faked.
"""

from __future__ import annotations

import logging

from sellee.mail import gmail, handoff

log = logging.getLogger(__name__)

# What a probe concluded, beyond the login state itself.
PROVIDER_OK = "ok"
PROVIDER_WRONG = "wrong_provider"

# Copy. Read on a phone, acted on at a desktop — so each says where the window is rather than
# assuming the seller is sitting in front of it.
#
# Deliberately *not* templated on a marketplace display name. Every existing sign-in notice speaks
# one session per market ("I'm reading that market again"), and a mailbox is a second session behind
# the same market: a seller whose site is in and whose mailbox is out needs to be told which one.
MAIL_SIGNED_IN_NOTICE = (
    "✅ Signed in to the mailbox for {name}. Next I'll check that I can see just your {name} mail "
    "and nothing else."
)
MAIL_SIGN_IN_HERE_NOTICE = (
    "{name} buyers email you rather than messaging in an app, so I need to read the mailbox that "
    "receives it. Its sign-in page is open in my Chrome window{where} — sign in there, then tap "
    "Check again. I only ever look at a search that shows your {name} mail."
)
MAIL_STILL_OUT_NOTICE = (
    "I still see a sign-in screen on that mailbox. Finish signing in on that tab, then tap Check "
    "again."
)
WRONG_PROVIDER_NOTICE = (
    "That mailbox is {found}, and {expected} is the only one I can read so far — driving a webmail "
    "I haven't been measured against would mean guessing at your buyers' messages. Sign in to the "
    "{expected} account that receives your {name} mail, or leave {name} listing-only for now."
)
VIEW_UNREADABLE_NOTICE = (
    "I signed in to that mailbox but couldn't read the search that shows your {name} mail — "
    "{reason}. Nothing is set up yet; tap below and I'll try again."
)
MAIL_READY_NOTICE = (
    "✅ {name} is fully connected. I'll watch that mailbox, answer buyers there, and ask each one "
    "to carry the thread to {address} so it doesn't expire on {name}'s schedule."
)


def probe_provider(answer: object) -> str:
    """Whether the mailbox the seller signed into is one this transport can drive.

    Read from what the probe actually found rather than from what was navigated: the seller may
    have signed a different account into that window, and the whole point of asking is that we do
    not assume.
    """
    found = (answer or {}).get("provider") if isinstance(answer, dict) else None
    return PROVIDER_OK if found == gmail.PROVIDER else PROVIDER_WRONG


def derive_handoff(address: object) -> str:
    """The handoff address for a mailbox: that mailbox's own address.

    Derived rather than asked, because the mailbox that receives the relay mail *is* the address on
    the marketplace account — there is nothing for the seller to decide and nothing to mistype.

    **No `+tag`, and that is a correction.** An earlier version appended one, so the read scope
    could be a single address that existed only for this marketplace. It was reverted for a reason
    that outranks scoping: the tag has to *deliver*, and a provider that ignores subaddressing
    turns the invitation into an address nobody is listening to — a buyer lost silently, which is
    worse than a buyer never invited. Scoping is a content question instead (`gmail.search_view`).
    """
    try:
        return handoff.parse(address)
    except handoff.HandoffError:
        return ""


def read_scoped_view(client, view: str, handoff_address: str = ""):
    """Read the scoped view in a fresh tab, and answer what came back.

    A **fresh tab** and not the shared one, because Gmail's row cache makes the scope guarantee
    false in a tab that has shown anything else — measured, see `BrowserClient.fresh_tab`. This is
    the step that proves the seller's mailbox can be read the narrow way before anything is
    recorded as connected.
    """
    with client.fresh_tab():
        client.navigate(view)
        return client.evaluate(gmail.message_list_js(handoff_address)) or {}


def view_is_readable(answer: object) -> bool:
    """Whether a scoped-view read succeeded — including the honestly empty case.

    A mailbox with no buyer mail yet is the **normal** state at connect time, and the artifact
    reports it as `empty_stated` rather than as a failure precisely so this does not refuse a new
    seller. Only an `{error}` is a failure.
    """
    if not isinstance(answer, dict):
        return False
    return "error" not in answer
