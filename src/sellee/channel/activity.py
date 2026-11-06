"""Activity updates: telling the seller what Sellee did on their listings, soon after it did it.

The words are `sellee.updates`' (pure, composed from the tells ledger); this decides when they go.
A tell opens a hold, and whatever else lands on the seller during it goes in the same message: the
update is due once the newest untold tell has sat for `HOLD_SEC`, or the oldest for `MAX_HOLD_SEC`
when tells keep arriving. Nothing is composed while paused, unbound or inside quiet hours, so a
night's tells become one update when the window ends rather than one queued per tell.

The hold also runs from the last update, so tells an update had no room for wait for the next one
rather than following it a minute later. An update due just before quiet hours goes then: the drain
delivers it within seconds, so it never waits out the night with the evening's words.

The notice and the stamp that marks its tells told are one transaction, and the notice's ref names
the oldest tell it carries, so a restart can neither lose an update nor send it twice.

The lane also writes the tells nothing else does, since each is only time passing: a buyer left
waiting on the seller's answer, a listing nobody has written about, a sale the seller is closing
themselves (store/slow_tells.py). They are written while unbound too, so `/catchup` can tell them.
And on the seller's Saturday, a seller told nothing for a week hears that their listings are still
up — but never beside an update still in its hold, which would reach them minutes later saying
much the same.

A quiet listing never makes an update due by itself: a text that arrives only because a listing
turned a week old reads as a timer, not as news. Its line rides the next update something else
makes due, or folds into the weekly line; one that meets neither before it goes stale is retired
untold, and its listing is still named in the weekly.

A seller who asks hears whatever is untold, hold and setting aside. The `/catchup` typed in the
chat queues it as a notice of its own (`queue_at_catchup`), so the drain delivers it, retried, with
its buttons; the attended session's hands it over in its result (`tell_at_catchup`), which is also
how an unbound seller hears at all.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from sellee import clock, settings, updates
from sellee.channel import outbound

UPDATES_LANE_INTERVAL_SEC = 60.0
# How long a tell waits for others to join it: long enough for a buyer's second lowball, short
# enough that the buyer is still there when the seller answers.
HOLD_SEC = 15 * 60.0
# How long a busy listing can keep pushing the update back before it goes anyway.
MAX_HOLD_SEC = 90 * 60.0
UPDATE_REF_PREFIX = "update:"
UPDATE_QUEUED_EVENT = "activity_update.queued"
# The weekly line: on the seller's Saturday, to a seller no update has reached in a week.
WEEKLY_REF_PREFIX = "update-weekly:"
WEEKLY_QUEUED_EVENT = "activity_update.weekly"
WEEKLY_WEEKDAY = 5  # Saturday, as datetime.weekday() counts
WEEKLY_QUIET_SEC = 7 * 24 * 3600
# A catchup whose read lost a race with the lane reads once more, so the seller who asked still
# hears whatever the lane left untold.
_CATCHUP_ATTEMPTS = 2


def updates_lane(*, store, bus=None, now: float | None = None) -> None:
    """Write the slow tells now due, then queue one update when one is due, then the weekly line
    when it is due and no update is on its way. The seller's `activity_updates` setting narrows
    what is sent: `decisions` keeps only the tells that ask them something, `off` keeps only the
    check-in on a sale they are closing themselves, which they asked for by taking it over. Either
    way, a tell no longer worth telling (stale, its listing gone, its premise gone, unreadable) is
    retired here, and a tell the setting holds back stays untold for `/catchup` until it goes
    stale."""
    if store.is_paused():
        return
    now = time.time() if now is None else now
    store.write_slow_tells(now=now)
    if store.get_channel()["chat_id"] is None:
        return
    if outbound.in_quiet_hours(store, now):
        return
    wanted = _queue_due_update(store, bus, now)
    if all(_rides_along(t) for t in wanted):
        _queue_weekly(store, bus, now, riders=wanted)


def _queue_due_update(store, bus, now: float) -> tuple[updates.TellableRecord, ...]:
    """Queue the update when it is due; the untold tells the seller wants either way, told now or
    still in their hold, so nothing else goes out ahead of them. Only the tells that do not ride
    along decide whether it is due, but once it is, it carries them all."""
    tells, context, selection = _select(store, now)
    wanted = _wanted(selection.tellable, tells, settings.get(store, "activity_updates"))
    due_by = tuple(t for t in wanted if not _rides_along(t))
    last_update = store.last_update_ts()
    since = max(last_update or 0.0, now - updates.STALE_AFTER_SECONDS)
    first_owed = store.first_owed_ts(due_by, since=since)
    if not _is_due(due_by, now, last_update=last_update, first_owed=first_owed):
        return wanted
    only_wanted = updates.TellSelection(tellable=wanted, retired_ids=())
    update = updates.compose_update(only_wanted, context, now=now)
    if update is None or not update.carried_ids:
        return wanted
    ref = _ref(update)
    notice_id = store.queue_update_notice(
        update.text, ref=ref, tell_ids=update.carried_ids, options=update.options, now=now
    )
    if notice_id is not None and bus is not None:
        bus.publish(
            UPDATE_QUEUED_EVENT,
            {
                "notice_id": notice_id,
                "ref": ref,
                "tells": len(update.carried_ids),
                "asks": bool(update.options),
            },
        )
    return wanted


def _rides_along(tell: updates.TellableRecord) -> bool:
    """Whether `tell` waits for something else to carry it rather than making an update due."""
    return tell["kind"] == updates.TELL_LISTING_QUIET


def _queue_weekly(store, bus, now: float, *, riders: Sequence[updates.TellableRecord] = ()) -> None:
    """The weekly line, once per ISO week of the seller's own calendar: on their Saturday, after a
    quiet week with something live. FYI only, so it goes only to a seller who wants every update.
    The quiet-listing tells waiting on it (`riders`) fold into it and are stamped told by it, so
    no update tells them again."""
    if settings.get(store, "activity_updates") != settings.ACTIVITY_UPDATES_ALL:
        return
    zone = (store.get_seller_config_section("basics") or {}).get("timezone") or ""
    day = clock.seller_datetime(zone, now)
    if day.weekday() != WEEKLY_WEEKDAY:
        return
    year, week, _weekday = day.isocalendar()
    ref = f"{WEEKLY_REF_PREFIX}{year}-W{week:02d}"
    if store.has_notice_with_ref(ref):
        return
    listings = store.live_listings()
    if not _had_a_quiet_week(store, listings, now):
        return
    quiet_items = {t["item_id"] for t in riders}
    text = updates.compose_weekly(
        [listing["title"] for listing in listings],
        buyers=store.buyers_who_wrote_since(now - WEEKLY_QUIET_SEC),
        aged=[
            listing["title"]
            for listing in listings
            if now - listing["live_since_ts"] >= WEEKLY_QUIET_SEC
        ],
        quiet=[listing["title"] for listing in listings if listing["item_id"] in quiet_items],
    )
    if text is None:
        return
    notice_id = store.queue_update_notice(
        text, ref=ref, tell_ids=[t["id"] for t in riders], holdable=True, now=now
    )
    if notice_id is not None and bus is not None:
        bus.publish(
            WEEKLY_QUEUED_EVENT,
            {"notice_id": notice_id, "ref": ref, "live": len(listings), "tells": len(riders)},
        )


def _had_a_quiet_week(store, listings, now: float) -> bool:
    """Whether a week has gone by with something live and no update. The week runs from the last
    update or from when the earliest live listing went up, whichever is later: a seller whose
    listings went up yesterday has not had a quiet week, update or none."""
    if not listings:
        return False
    quiet_since = min(listing["live_since_ts"] for listing in listings)
    last_update = store.last_update_ts()
    if last_update is not None:
        quiet_since = max(quiet_since, last_update)
    return now - quiet_since >= WEEKLY_QUIET_SEC


def tell_at_catchup(store, *, now: float | None = None) -> dict | None:
    """Hand over everything untold, as the text of one update, and record it told by a notice
    delivered via catchup; None when nothing is untold. The seller asked, so neither the hold nor
    the setting applies — the setting is about what is sent unasked. `asks` says whether the update
    wants a decision, which a catchup can only take in words."""
    now = time.time() if now is None else now
    update = _hand_over(
        store,
        now,
        lambda u, ref: store.record_update_at_catchup(
            u.text, ref=ref, tell_ids=u.carried_ids, now=now
        ),
    )
    return None if update is None else {"text": update.text, "asks": bool(update.options)}


def queue_at_catchup(store, *, now: float | None = None) -> bool:
    """Queue everything untold as one update for the drain to deliver, hold and setting aside, and
    stamp it told by that notice; whether there was anything to queue. Not holdable, since the
    seller asked, and it keeps its buttons. Queued rather than handed back because the reply it
    rides on is sent once with no retry, and a stamp taken before that send could lose it."""
    now = time.time() if now is None else now
    update = _hand_over(
        store,
        now,
        lambda u, ref: store.queue_update_notice(
            u.text, ref=ref, tell_ids=u.carried_ids, options=u.options, holdable=False, now=now
        ),
    )
    return update is not None


def untold_text(store, *, now: float | None = None) -> str | None:
    """Everything untold as the text of one update, recording no notice and stamping nothing told:
    a preview for when nothing could deliver it (paused), so it is still untold once the seller
    resumes. Tells no longer worth telling are retired, as on any read of the ledger."""
    now = time.time() if now is None else now
    _tells, context, selection = _select(store, now)
    update = updates.compose_update(selection, context, now=now)
    return None if update is None or not update.carried_ids else update.text


def _hand_over(
    store, now: float, write: Callable[[updates.Update, str], int | None]
) -> updates.Update | None:
    """Compose everything untold and `write` it under its ref; the update written, or None when
    nothing is untold. A write that loses a race with the lane reads once more for what is left."""
    for _attempt in range(_CATCHUP_ATTEMPTS):
        _tells, context, selection = _select(store, now)
        update = updates.compose_update(selection, context, now=now)
        if update is None or not update.carried_ids:
            return None
        if write(update, _ref(update)) is not None:
            return update
    return None


def _select(store, now: float):
    """The untold tells, what their words depend on now, and which of them are worth telling. The
    rest, and any whose premise has gone, are retired on the spot, so they are never read again."""
    store.retire_moot_tells(now=now)
    tells = store.untold_tells()
    context = store.update_context(
        item_ids=[t["item_id"] for t in tells if t["item_id"]],
        thread_ids=[t["thread_id"] for t in tells if t["thread_id"]],
    )
    selection = updates.select_tellable(tells, context, now=now)
    store.mark_tells_told(list(selection.retired_ids), notice_id=None, now=now)
    return tells, context, selection


def _wanted(
    tellable: Sequence[updates.TellableRecord],
    tells: Sequence[updates.TellRecord],
    mode: object,
) -> tuple[updates.TellableRecord, ...]:
    if mode == settings.ACTIVITY_UPDATES_OFF:
        return tuple(t for t in tellable if t["kind"] == updates.TELL_DEAL_CHECKIN)
    if mode == settings.ACTIVITY_UPDATES_DECISIONS:
        asking = {t["id"] for t in tells if t["decision"]}
        return tuple(t for t in tellable if t["id"] in asking)
    return tuple(tellable)


def _is_due(
    tells: Sequence[updates.TellableRecord],
    now: float,
    *,
    last_update: float | None,
    first_owed: float | None,
) -> bool:
    """Whether the hold is up. The cap runs from `first_owed` rather than the oldest tell, since a
    buyer's counter replaces their untold offer and would otherwise restart it every time."""
    if not tells:
        return False
    if last_update is not None and now - last_update < HOLD_SEC:
        return False
    newest = max(t["created_ts"] for t in tells)
    oldest = min(t["created_ts"] for t in tells)
    if first_owed is not None:
        oldest = min(oldest, first_owed)
    return now - newest >= HOLD_SEC or now - oldest >= MAX_HOLD_SEC


def _ref(update: updates.Update) -> str:
    return f"{UPDATE_REF_PREFIX}{min(update.carried_ids)}"
