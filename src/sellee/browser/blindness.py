"""What the read lane can honestly say about why it cannot see a market.

One notice used to cover every way a read could fail, and it ended with *"Check that the agent's
Chrome is running and still logged in."* On 2026-08-28 a seller got that sentence 126 reads into a
28-hour outage whose cause was the daemon's own browser subprocess, while their Chrome sat in front
of them signed in to Carousell with sixteen unread chats. The sentence was not merely unhelpful: it
was advice for a condition that, when it is actually true, produces a *different* notice — a Chrome
that is genuinely down never reaches the blind counter at all, because acquiring the browser probes
it first and raises `BrowserUnavailable` with the command to start it.

So the causes are separated here, and each one only claims what the lane has evidence for:

  * **plumbing** — our own server lost Chrome. Chrome answered its probe; our calls did not.
  * **market** — the marketplace would not hand over the conversation list. Page JS ran, so
    everything from Chrome to the page load is provably fine, and it answered with an error.
  * **tails** — we can see the inbox and cannot read the conversations in it. Also our own fault
    most of the time, and deliberately *not* merged with `market`: a 403 on the list is Carousell
    refusing us, while an unreadable tail is our reader failing on a page Carousell served fine.
    One sentence covering both would blame the marketplace for our own drift, which is the same
    species of lie this module exists to delete.
  * **viewport** — the window is too narrow for the marketplace to render the thing we read. The
    one cause the *seller* can fix, so it is separated from `market`.

None of them tells the seller to go and look at Chrome. That sentence lives on, once, in
`chrome_hint` — for the one caller that reaches a state where it might be true.

What is deliberately absent is a fourth cause for "Chrome went away mid-tick". The notice for that
already exists, already carries the argv or the container's start script, and is better than
anything this module could say; a read that discovers it mid-tick just counts, and the next
acquisition says it properly.
"""

from __future__ import annotations

from sellee import deployment

CAUSE_PLUMBING = "plumbing"
CAUSE_MARKET = "market"
CAUSE_TAILS = "tails"
CAUSE_VIEWPORT = "viewport"
CAUSE_VERIFY = "verify"

# Below this, a marketplace is liable to serve a layout we cannot read — and the failure looks
# exactly like the marketplace refusing us, which is what makes it worth naming separately. The
# number is a marketplace's own responsive breakpoint, not a guess, and only a floor: it never
# claims a read failed, it only reframes one that already did.
MIN_USABLE_WIDTH_PX = 900

# Claims only what is evidenced: that Chrome is answering us, and that reads have stopped. Not that
# the seller is signed in — no login probe ran, because the navigate before it failed. Not that the
# network is fine either: the 2026-08-27 wedge began with `net::ERR_INTERNET_DISCONNECTED`, so "this
# one is on my side" would have been its own wrong guess, with the blame merely moved one hop.
PLUMBING_NOTICE = (
    "I've lost my own connection to Chrome, so I'm not reading your {name} inbox and I may be "
    "missing buyer messages. Chrome itself is answering me, so there's nothing for you to restart. "
    "I keep replacing my side of the connection and it keeps dropping — I'll tell you as soon as "
    "I'm reading that market again."
)

# The marketplace answered, and said no. Page JS ran, so this is the one cause where we know the
# whole chain up to and including the page is working.
MARKET_NOTICE = (
    "I can't read your {name} inbox right now, so I may be missing buyer messages. My Chrome is up "
    "and the page loads for me — it's {name} that won't hand over your conversations. I'll keep "
    "trying, and I'll tell you when I'm through. Until then your {name} app has the messages I "
    "can't see."
)

# We are looking at the inbox and cannot read what is in it. Says which, because a count that never
# names a conversation is what made this undiagnosable for a day.
TAILS_NOTICE = (
    "I can see your {name} inbox but I can't read the conversations in it, so I may be missing "
    "buyer messages — {count} of them wouldn't open on my last look. Your {name} app has anything "
    "I've missed. This one's mine to fix and I'm on it."
)

# The close-out. Sent only when the seller was actually warned, only when the gap was long enough to
# have mattered, and only on a read that got message content back — never merely because the
# conversation list answered, which is a tick that can report success having read nothing at all.
READING_AGAIN_NOTICE = (
    "I'm reading your {name} inbox again — I was blind to it for {how_long}. Anything that came in "
    "while I couldn't see is in front of me now."
)

# Below this, a recovery is not worth a message: the lane blinked and fixed itself, and the seller
# was never waiting. Above it they may well have been, and the last thing they heard was that the
# market was unreadable.
GAP_WORTH_MENTIONING_SEC = 1800.0

# Which Chrome to go and look at, for the one caller that still has a reason to ask. On a host
# install it is the window the agent opens for itself; in a container it is the seller's own, on
# their own desktop, and closing it is the most likely reason anything is wrong.
CHROME_CHECK = "Check that the agent's Chrome is running and still logged in."
CONTAINER_CHROME_CHECK = (
    "Check that Chrome is running on your own computer (start it with ./start-chrome.sh) and "
    "still logged in."
)

# The one the seller can fix, so it is the one that asks them to. Names the size because "wider"
# alone invites a nudge of a few pixels.
VIEWPORT_NOTICE = (
    "I can't read your {name} inbox, and I think it's the window: my Chrome{where} is {width}px "
    "wide and {name} needs about {needed}px to lay the page out the way I read it. Drag that "
    "window wider — or full-screen it — and I'll pick up on my next look, in a few minutes. "
    "Until then your {name} app has anything I've missed."
)

# Also the seller's to fix, and the only one where the marketplace is asking *them* a question —
# a verification wall in front of the messages looks from the lane like the marketplace refusing
# us. What the wall actually asks for is the market's own business, so an adapter that reports
# this cause ships its own wording (`verify_notice`); this is the fallback for one that does not,
# and it names only what any wall evidences.
VERIFY_NOTICE = (
    "I can't read your {name} messages — {name} is asking you to verify something before it will "
    "show them. That one's yours to answer: open my Chrome{where}, do it there, and I'll pick "
    "them up on my next look. Until then your {name} app has anything I've missed."
)

_NOTICES = {
    CAUSE_PLUMBING: PLUMBING_NOTICE,
    CAUSE_MARKET: MARKET_NOTICE,
    CAUSE_TAILS: TAILS_NOTICE,
    CAUSE_VIEWPORT: VIEWPORT_NOTICE,
    CAUSE_VERIFY: VERIFY_NOTICE,
}


def cause_for(cause: str, measured: dict | None) -> str:
    """Promote a failed read to `viewport` when the reader says the window was too narrow.

    The lane cannot tell these apart by itself — an unreadable layout and a refusal both arrive as
    "the list did not answer" — so this consults the width every reader reports. Only ever promotes,
    and only from a cause about the market: a plumbing failure stays plumbing however narrow the
    window. A verification wall wins over the window, because the width measured behind a PIN
    prompt is the prompt's.
    """
    if cause not in (CAUSE_MARKET, CAUSE_TAILS):
        return cause
    if (measured or {}).get("blocked") == CAUSE_VERIFY:
        return CAUSE_VERIFY
    width = (measured or {}).get("width")
    if isinstance(width, bool) or not isinstance(width, (int, float)):
        return cause
    return CAUSE_VIEWPORT if 0 < width < MIN_USABLE_WIDTH_PX else cause


def notice_for(
    cause: str,
    *,
    name: str,
    count: int = 0,
    width: int = 0,
    where: str = "",
    verify_notice: str = "",
) -> str:
    """The sentence for one cause, addressed to the seller.

    `verify_notice` is the market's own wording for its verification wall, used only on that
    cause — the wall asks for something market-specific (Facebook: a Messenger PIN) that a shared
    sentence cannot name without lying about the others.

    Falls back to the marketplace wording for an unknown cause: of them all it is the only one that
    asserts nothing about our own machinery, so a cause nobody has taught this module yet cannot
    make us claim a fault we have not established.
    """
    template = _NOTICES.get(cause, MARKET_NOTICE)
    if cause == CAUSE_VERIFY and verify_notice:
        template = verify_notice
    return template.format(
        name=name, count=count, width=int(width or 0), needed=MIN_USABLE_WIDTH_PX, where=where
    )


def gap_text(seconds: float) -> str:
    """How long we were blind, rounded to something a person would say.

    It is in the recovery notice because it is the one operationally useful fact in it: it tells the
    seller how far back to scroll in the marketplace's own app.
    """
    minutes = max(1, int(round(seconds / 60.0)))
    if minutes < 60:
        return f"about {minutes} minute{'s' if minutes != 1 else ''}"
    hours = int(round(minutes / 60.0))
    if hours < 48:
        return f"about {hours} hour{'s' if hours != 1 else ''}"
    return f"about {int(round(hours / 24.0))} days"


def chrome_hint(chrome_up: bool) -> str:
    """The go-and-look-at-Chrome sentence, or nothing at all when Chrome has just answered us.

    A hint the seller cannot act on is worse than no hint: it sends them to check the one thing we
    already know is fine, and buys the real fault another evening.
    """
    if chrome_up:
        return ""
    return CONTAINER_CHROME_CHECK if deployment.is_container() else CHROME_CHECK
