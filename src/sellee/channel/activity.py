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

A seller who asks hears whatever is untold, hold and setting aside. The `/catchup` typed in the
chat queues it as a notice of its own (`queue_at_catchup`), so the drain delivers it, retried, with
its buttons; the attended session's hands it over in its result (`tell_at_catchup`), which is also
how an unbound seller hears at all.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from sellee import settings, updates
from sellee.channel import outbound

UPDATES_LANE_INTERVAL_SEC = 60.0
# How long a tell waits for others to join it: long enough for a buyer's second lowball, short
# enough that the buyer is still there when the seller answers.
HOLD_SEC = 15 * 60.0
# How long a busy listing can keep pushing the update back before it goes anyway.
MAX_HOLD_SEC = 90 * 60.0
UPDATE_REF_PREFIX = "update:"
UPDATE_QUEUED_EVENT = "activity_update.queued"
# A catchup whose read lost a race with the lane reads once more, so the seller who asked still
# hears whatever the lane left untold.
_CATCHUP_ATTEMPTS = 2


def updates_lane(*, store, bus=None, now: float | None = None) -> None:
    """Queue one update when one is due. The seller's `activity_updates` setting narrows what is
    sent: `decisions` keeps only the tells that ask them something, `off` sends nothing. Either
    way, a tell no longer worth telling (stale, its listing gone, unreadable) is retired here, and
    a tell the setting holds back stays untold for `/catchup` until it goes stale."""
    if store.is_paused() or store.get_channel()["chat_id"] is None:
        return
    now = time.time() if now is None else now
    if outbound.in_quiet_hours(store, now):
        return
    tells, context, selection = _select(store, now)
    wanted = _wanted(selection.tellable, tells, settings.get(store, "activity_updates"))
    last_update = store.last_update_ts()
    since = max(last_update or 0.0, now - updates.STALE_AFTER_SECONDS)
    first_owed = store.first_owed_ts(wanted, since=since)
    if not _is_due(wanted, now, last_update=last_update, first_owed=first_owed):
        return
    only_wanted = updates.TellSelection(tellable=wanted, retired_ids=())
    update = updates.compose_update(only_wanted, context, now=now)
    if update is None or not update.carried_ids:
        return
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
    rest are retired on the spot, so they are never read again."""
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
        return ()
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
