"""Does this process's clock agree with the seller's own?

Everything time-of-day in the agent is decided against the daemon's local wall clock — the quiet
window that holds back a publish, and the one that holds back a buzz on the seller's phone. That
is the right rule when the daemon runs on the seller's own machine, because then there is only
one clock.

A container has its own, and it defaults to UTC. An eight-hour offset does not fail anything
loudly: it silently moves a 22:00–08:00 quiet window onto the wrong eight hours, so the agent
publishes and messages at four in the morning and everything looks like it worked. So the two are
compared at startup and the disagreement is said out loud, in hours, with the fix.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

MISMATCH_NOTICE = (
    "My clock is {offset} from {zone}, where you sell. Quiet hours and anything else timed to "
    "your day would be that far out, so I've flagged it rather than guessed. Set TZ={zone} for "
    "the container and restart it."
)

_MINUTES_PER_HOUR = 60


def local_offset_minutes(now: float) -> int:
    """This process's UTC offset right now, in minutes."""
    return int(datetime.fromtimestamp(now).astimezone().utcoffset().total_seconds() // 60)


def zone_offset_minutes(zone: str, now: float):
    """A named zone's UTC offset right now, or None when the name is not one we can resolve."""
    if not zone:
        return None
    try:
        info = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    moment = datetime.fromtimestamp(now, tz=timezone.utc).astimezone(info)
    return int(moment.utcoffset().total_seconds() // 60)


def offset_delta(zone: str, now: float):
    """How far this process's clock is from the seller's, in minutes; 0 when they agree.

    None means the question could not be asked — no recorded timezone, or a name the zone
    database does not have — which is not a mismatch and must not be reported as one.
    """
    theirs = zone_offset_minutes(zone, now)
    if theirs is None:
        return None
    return local_offset_minutes(now) - theirs


def render_delta(minutes: int) -> str:
    """A signed offset as something a person reads: "8 hours behind", "30 minutes ahead"."""
    direction = "behind" if minutes < 0 else "ahead of"
    magnitude = abs(minutes)
    if magnitude % _MINUTES_PER_HOUR == 0:
        hours = magnitude // _MINUTES_PER_HOUR
        amount = f"{hours} hour" if hours == 1 else f"{hours} hours"
    else:
        amount = f"{magnitude} minutes"
    return f"{amount} {direction}"


def check_timezone(*, store, bus, now=None) -> bool:
    """Compare the clock against the seller's recorded timezone; say so once if they disagree.

    Answers whether a mismatch was found. Called at startup rather than per tick: the container's
    zone is fixed for the life of the process, so there is nothing new to learn by asking again.
    """
    now = time.time() if now is None else now
    zone = (store.get_seller_config_section("basics") or {}).get("timezone") or ""
    delta = offset_delta(zone, now)
    if not delta:
        return False
    rendered = render_delta(delta)
    log.warning("this process's clock is %s %s — timing decisions will be off", rendered, zone)
    store.queue_notice(MISMATCH_NOTICE.format(offset=rendered, zone=zone))
    bus.publish("clock.timezone_mismatch", {"zone": zone, "offset_minutes": delta})
    return True
