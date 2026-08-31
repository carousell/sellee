"""The inbox read lane: see what buyers said, without spending a single token on it.

Each tick the lane asks the marketplace which conversations exist, opens the ones that look like
they moved, reads each one's messages, and reconciles them against the rows already stored — a
navigate and one JS evaluate per thread, no model turns. By the time a reply pass runs, what the
buyer said is already state.

The conversation list is the marketplace's own API where it has one, and the message read is DOM
work: identity, the counterpart, the listing and the unread count are facts we want typed and loudly
wrong when they change; message bubbles are only ever on a page.

Three rules keep the lane honest:

  * A market that cannot be seen must never look like a market with no news. A failed read is
    counted, and a run of them raises one needs-me notice rather than passing for silence.
  * The skip gate is an optimization, never a correctness input. A thread whose last message we
    already hold is left closed this tick — but every Nth tick opens everything regardless, so a
    stale list costs one sweep interval of latency and nothing more.
  * Reading never advances the reply cursor. Only a committed reply does, so a crash between seeing
    a message and answering it leaves the buyer waiting rather than silently handled.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from sellee import marketplaces, settings
from sellee.browser import blindness, reconcile
from sellee.browser import markets as market_adapters
from sellee.browser.client import BrowserDetached, BrowserError, BrowserUnavailable
from sellee.channel import fastpaths
from sellee.engines import hosts
from sellee.engines import pacing as pacing_engine
from sellee.engines import scam as scam_engine
from sellee.store import StoreError

log = logging.getLogger(__name__)

# Statuses whose threads are still worth opening. A held or escalated thread is waiting on someone
# else, and a terminal one is over; reading them would only mark them read on the platform.
_ACTIVE_STATUSES = ("active", "liaising", "agreed")

# Pass types that drive the browser, and so must not overlap the lane's reads.
_BROWSER_PASS_TYPES = ("reply", "publish")

LOGGED_OUT_NOTICE = (
    "Your {name} session is signed out, so I've stopped reading that market. Tap below and I'll "
    "open the sign-in page in my Chrome for you — I never sign in for you."
)
# The notice is read on a phone, so the way out of it has to be reachable from one. It used to
# name `sellee connect <market>` at a shell, which is on a desktop the seller may be nowhere near
# — the market stayed dead until they happened to sit down at it. The button hands the same job to
# the connect lane; the CLI is still there, and browser/connect.py names it in the one case where
# it is the remaining option (the lane could not drive Chrome at all).
UNAVAILABLE_NOTICE = (
    "I can't drive the browser at the moment, so browser marketplaces are paused. "
    "The carousell.ai side is unaffected. Details: {reason}"
)


@dataclass
class InboxDeps:
    store: object
    bus: object
    config: object
    browser_factory: object
    # Lane state, in process on purpose: it is all counters, and a restart re-arming them errs
    # toward reading more rather than less.
    ticks: dict = field(default_factory=dict)
    blind: dict = field(default_factory=dict)
    notified: dict = field(default_factory=dict)
    # When each market last went blind, so a recovery can say how long it lasted — and, because it
    # is only set on the first failure of a run, so a market that flaps does not keep resetting it.
    blind_since: dict = field(default_factory=dict)
    now: Callable[[], float] = time.time


def seller_region(store) -> str | None:
    return store.seller_region()


def _notify_once(deps: InboxDeps, key: str, text: str, controls: list | None = None) -> None:
    """Queue a needs-me notice at most once per condition, so a lane that keeps failing keeps
    telling the event log and stops telling the seller."""
    if deps.notified.get(key):
        return
    deps.notified[key] = True
    deps.store.queue_notice(text, controls=controls)


def _clear_notice(deps: InboxDeps, key: str) -> None:
    deps.notified.pop(key, None)


def _unavailable(deps: InboxDeps, exc: BrowserUnavailable) -> None:
    deps.bus.publish("browser.unavailable", {"reason": str(exc)})
    _notify_once(deps, "unavailable", UNAVAILABLE_NOTICE.format(reason=exc))


def browser_pass_running(store) -> bool:
    """Whether a pass that drives Chrome is queued or running.

    A rail publish never touches the browser, so it is not a reason to yield; a browser-market one
    holds the tab for minutes where the lane holds it for seconds. Worst case the lane notices a
    buyer message one tick late.
    """
    for row in store.active_passes_of_types(_BROWSER_PASS_TYPES):
        if row["type"] != "publish":
            return True
        market = (row.get("payload") or {}).get("market")
        if market is None or marketplaces.connector_type(market) == "browser":
            # An unnamed market predates the browser publish path; treat it as browser-touching
            # rather than assume the safe case.
            return True
    return False


def inbox_lane(deps: InboxDeps) -> None:
    """One tick: read every browser market's inbox and fold what is new into durable rows."""
    if deps.store.is_paused():
        return
    if browser_pass_running(deps.store):
        return
    try:
        client = deps.browser_factory()
    except BrowserUnavailable as exc:
        _unavailable(deps, exc)
        return

    region = seller_region(deps.store)
    # Read at use, every tick: the markets the seller has connected, never every market we happen to
    # have an adapter for. A marketplace they have not connected — or have just removed — is not one
    # to open, probe, or tell them they are signed out of.
    for market in settings.connected_markets(deps.store):
        adapter = market_adapters.get_adapter(market)
        if adapter is None:
            continue  # a registry entry with no adapter yet is not a market we can read
        try:
            with client.exclusive():
                _read_market(deps, client, adapter, region)
        except BrowserUnavailable as exc:
            # The same absence the factory reports, discovered one step later (the binary is
            # there but the server dies at startup). Every browser market is equally unreadable,
            # and the seller needs the install hint, not a Chrome check — so this is never fed
            # to the blind counter.
            _unavailable(deps, exc)
            return
        except BrowserDetached as exc:
            # Our own server lost Chrome. The factory has already tried to replace it; what reaches
            # here is a tick that could not run, and the one thing it must not do is send the seller
            # to check a browser that answered its probe moments ago.
            _count_blind(deps, market, str(exc), cause=blindness.CAUSE_PLUMBING)
        except BrowserError as exc:
            _count_blind(deps, market, str(exc))
    # Recovery is a tick that ran into no unavailability, so a condition that persists mid-loop
    # keeps its one notice instead of being re-queued every tick.
    _clear_notice(deps, "unavailable")


def _read_market(deps: InboxDeps, client, adapter, region: str | None) -> None:
    market = adapter.market
    inbox_url = marketplaces.market_url(market, "inbox", region)
    if inbox_url is None:
        log.warning("no recorded inbox URL for %s — skipping", market)
        return

    client.navigate(inbox_url)
    login = client.evaluate(adapter.login_js) or {}
    state = login.get("state")
    if state == "logged_out":
        deps.bus.publish("browser.login", {"market": market, "state": state})
        _notify_once(
            deps,
            f"logged_out:{market}",
            LOGGED_OUT_NOTICE.format(name=marketplaces.display_name(market)),
            controls=fastpaths.signin_controls(market),
        )
        return
    # Only a confirmed sign-in re-arms the notice. `unknown` is "no answer" everywhere else in this
    # module, and clearing on it made the once-guard worthless in exactly the case it exists for: a
    # probe that flaps between logged_out and unknown re-armed on every flap, so a seller who was
    # signed out for two hours got the same message seven times instead of once.
    if state == "logged_in":
        _clear_notice(deps, f"logged_out:{market}")
        # The backfill half of the ask: reaches markets connected before the survey existed. The
        # probe has just run, so the login check costs nothing; the primary key makes it ask-once.
        if market_adapters.can_survey(market, region):
            deps.store.request_market_survey(market)

    answer = client.evaluate(adapter.conversations_list_js)
    if not isinstance(answer, dict) or not isinstance(answer.get("conversations"), list):
        # The list came back as a failure rather than as content. Unlike a DOM read that finds
        # nothing, this cannot be mistaken for an empty inbox, so it is reported as what it is.
        reason = (answer or {}).get("error") if isinstance(answer, dict) else "unreadable"
        _count_blind(
            deps, market, f"conversation list unavailable: {reason}", cause=blindness.CAUSE_MARKET
        )
        return
    rows = answer["conversations"]

    tick = deps.ticks.get(market, 0) + 1
    deps.ticks[market] = tick
    full_sweep = tick % max(1, int(deps.config.inbox_full_sweep_every)) == 0

    known = {t["thread_id"]: t for t in deps.store.list_threads(side="sell")}
    items = deps.store.list_items()
    # Threads holding a send we cannot account for. These are opened whatever their status and
    # whatever the list preview claims: an unconfirmed send escalates its thread, `escalated` is not
    # in _ACTIVE_STATUSES, and skipping it is what left the only reader that could answer the
    # question switched off by the act of asking it.
    unsettled = _unsettled_by_thread(deps.store)
    opened = 0
    recorded = 0
    unreadable = 0
    settling = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        thread_id = _thread_key(market, row.get("thread_id"))
        handle = str(row.get("handle") or "")
        if not thread_id or handle.lower() in adapter.system_handles:
            continue  # the platform talking to the seller, not a buyer
        thread = known.get(thread_id)
        if thread is None:
            if _adopt(deps, market, adapter, thread_id, row, handle, items):
                thread = deps.store.get_thread(thread_id)
            if thread is None:
                continue
        must_settle = thread_id in unsettled
        if must_settle:
            settling += 1
        elif thread["status"] not in _ACTIVE_STATUSES:
            continue
        elif not full_sweep and _can_skip(deps.store, thread, row):
            continue
        opened += 1
        fresh = _read_thread(deps, client, adapter, thread, region, row, unsettled)
        if fresh is None:
            unreadable += 1
        else:
            recorded += fresh

    deps.bus.publish(
        "browser.read",
        {
            "market": market,
            "rows": len(rows),
            "opened": opened,
            "recorded": recorded,
            "unreadable": unreadable,
            "full_sweep": full_sweep,
            # How many of the opened threads were opened to chase a send of our own, so a tick spent
            # settling rather than reading is legible in the event stream.
            "settling": settling,
        },
    )
    # Only a read where every opened thread was legible counts as seeing the market. A conversation
    # whose message list we could not find is the same class of failure as an unreadable inbox: it
    # would otherwise pass for "this buyer said nothing new", which is how one gets stranded.
    if unreadable:
        _count_blind(
            deps,
            market,
            f"{unreadable} conversation(s) unreadable",
            cause=blindness.CAUSE_TAILS,
            count=unreadable,
        )
    else:
        # `recorded` is not the test — a tick can legitimately open threads and find nothing new.
        # What makes this a market we can see is that every thread we opened was legible, which is
        # exactly what `unreadable == 0` on a tick that opened something means.
        _clear_blind(deps, market, read_content=opened > 0)


def _thread_key(market: str, native_id) -> str | None:
    if not native_id:
        return None
    return f"{market}:{native_id}"


def _can_skip(store, thread: dict, row: dict) -> bool:
    """Whether the conversation list still shows the message we already stored last.

    Only ever used to avoid opening a thread. Anything unread, anything whose last message we do not
    already have, and any thread we have never read is opened — the gate errs toward the read, and
    the periodic full sweep opens everything regardless.
    """
    if row.get("unread"):
        return False
    messages = store.get_thread_messages(thread["thread_id"], limit=1)
    if not messages:
        return False
    return reconcile.preview_matches(row.get("last_message") or "", messages[-1]["text"])


def _unmatched(deps: InboxDeps, market: str, thread_id: str, reason: str) -> None:
    deps.bus.publish(
        "browser.unmatched", {"market": market, "thread_id": thread_id, "reason": reason}
    )


def _unreadable(deps: InboxDeps, market: str, thread_id: str, reason: str) -> None:
    """Say which conversation could not be read, and which of the ways it failed.

    `_read_thread` answers None for three different things — the reader found no message list, the
    list read as empty for a conversation that is not, and the inbox claims unread content the tail
    does not hold — and the caller only ever counted them. So a market reporting "3 conversation(s)
    unreadable" every tick for a day was undiagnosable from the log: the count says how many, never
    which or why, and the three causes want three different fixes.
    """
    deps.bus.publish(
        "browser.unreadable",
        {"market": market, "thread_id": thread_id, "reason": reason[:200]},
    )


def _adopt(deps: InboxDeps, market: str, adapter, thread_id: str, row: dict, handle: str, items):
    """Create a thread for a buyer writing about one of our listings for the first time.

    Three things have to hold: the buyer approached us (`received` — not an offer we made), the
    conversation names a listing we recognise, and we know who they are. A thread attached to the
    wrong item would negotiate against the wrong floor, so anything less is left alone — most often
    it is simply a listing the seller made outside the agent.

    Every refusal says which of those failed, because this event is what answers "why is nobody
    answering this buyer". One label for all of them makes the ordinary case (a listing that is not
    ours) read like the alarming one (two of our items claiming the same listing).
    """
    if row.get("offer_type") not in (None, "", "received"):
        _unmatched(deps, market, thread_id, "we_offered")
        return False
    matches = reconcile.matching_items(
        row.get("product_id"), items, market, adapter.listing_id_pattern
    )
    if not handle:
        _unmatched(deps, market, thread_id, "no_handle")
        return False
    if len(matches) != 1:
        _unmatched(deps, market, thread_id, "unknown_listing" if not matches else "two_items")
        return False
    item_id = matches[0]
    try:
        deps.store.create_thread(
            thread_id=thread_id,
            side="sell",
            market=market,
            counterpart_handle=handle,
            item_id=item_id,
            source="browser_read",
        )
    except StoreError as exc:
        log.warning("could not create thread %s: %s", thread_id, exc)
        return False
    deps.bus.publish(
        "browser.thread_new", {"market": market, "thread_id": thread_id, "item_id": item_id}
    )
    return True


def _settle_unsettled(deps: InboxDeps, thread_id: str, tail: list, unsettled: dict) -> list:
    """Answer "did our own message actually land" for every unsettled send on this thread.

    The lane is already here with the conversation on screen, so this is unfinished work it can
    finish rather than a reason to ask the seller anything. A found bubble commits its intent — same
    deterministic msg_id a verified send would have used — and withdraws the `unconfirmed_send`
    escalation it caused; a miss only counts the attempt, which is what makes the eventual ask a
    last resort instead of a timeout. Never a re-send either way: the page is evidence about what
    happened, never permission to do it again.

    Runs BEFORE the reconciler so a settled reply is already stored when the tail is aligned —
    otherwise the very bubble we just recognised is inserted a second time as a phantom manual
    seller reply, which is what the live database's 34 `manual` rows against 13 `agent` ones look
    like.

    Returns the texts it settled. Storing them is not on its own enough to keep the reconciler off
    them: `new_rows` aligns the tail as a trailing window, so a settled outbound row sitting after
    an inbound one we have never stored leaves no matching suffix and the whole tail reads as new.
    The caller drops these texts from what it records for exactly that case.
    """
    settled = []
    for intent in unsettled.get(thread_id, ()):
        if reconcile.contains_outbound(tail, intent["text"]):
            result = deps.store.settle_intent_from_read(intent["intent_id"])
            if result is None:
                continue
            settled.append(intent["text"])
            deps.bus.publish(
                "intent.settled",
                {
                    "intent_id": result["intent_id"],
                    "thread_id": thread_id,
                    "escalations_resolved": len(result["escalations_resolved"]),
                },
            )
            for esc_id in result["escalations_resolved"]:
                deps.bus.publish("escalation.resolved", {"id": esc_id})
        else:
            deps.store.bump_verify_attempt(intent["intent_id"])
    return settled


def _unsettled_by_thread(store) -> dict:
    """Unsettled sends grouped by thread, read once per tick rather than per conversation."""
    grouped: dict = {}
    for intent in store.unsettled_intents():
        grouped.setdefault(intent["thread_id"], []).append(intent)
    return grouped


def _read_thread(
    deps: InboxDeps,
    client,
    adapter,
    thread: dict,
    region: str | None,
    row: dict | None = None,
    unsettled: dict | None = None,
) -> int | None:
    """Open one thread and reconcile its tail. Returns how many rows were new, or None when the
    conversation could not be read at all — which the caller counts as being blind on this market,
    never as the buyer having said nothing.

    `row` is the conversation as the list reported it, which makes two failures detectable: if the
    list says the conversation has a latest message and the page shows none, the two disagree and
    we cannot see what we were told is there; and if the list reports unread content but the
    reconciler finds nothing fresh, a repeat past the tail window would otherwise pass for a
    quiet buyer.
    """
    market = thread["market"]
    native = thread["thread_id"].split(":", 1)[1] if ":" in thread["thread_id"] else ""
    url = marketplaces.market_url(market, "thread", region, thread_id=native)
    if url is None:
        log.warning("no recorded thread URL template for %s", market)
        return None
    client.navigate(url)
    raw = client.evaluate(adapter.conversation_tail_js)
    unreadable = reconcile.unreadable_reason(raw)
    if unreadable is not None:
        # The reader could not find the message list. An empty tail would claim the conversation is
        # over; this says we could not see it — and now says why, which is the difference between a
        # marketplace that changed shape and a window too narrow to render the one we know.
        _unreadable(deps, market, thread["thread_id"], unreadable)
        return None
    tail = reconcile.classify_tail(raw)
    # Settle our own unconfirmed sends first, so anything recognised is stored before the tail is
    # aligned against what we have — see _settle_unsettled.
    just_settled = _settle_unsettled(deps, thread["thread_id"], tail, unsettled or {})
    stored = deps.store.get_thread_messages(thread["thread_id"], limit=None)
    if not tail:
        # A thread exists because somebody wrote in it, so a conversation we have already recorded
        # messages for cannot legitimately read as empty. Neither can one the list just told us has
        # a latest message. Either way the page changed shape under the reader — report that rather
        # than let it pass for "the buyer said nothing new".
        if stored or (row or {}).get("last_message"):
            _unreadable(
                deps,
                market,
                thread["thread_id"],
                "the message list read as empty for a conversation that is not",
            )
            return None
        return 0
    fresh = reconcile.new_rows(tail, stored, now=deps.now())
    if just_settled:
        # A message we just settled is already stored as ours. Recording it again would journal our
        # own reply as one the seller typed in the app, which is how a thread goes quiet: a `manual`
        # outbound row means "our account spoke last" and stops follow-ups.
        fresh = [
            entry
            for entry in fresh
            if not (
                entry["direction"] == "out"
                and any(reconcile.same_text(entry["text"], text) for text in just_settled)
            )
        ]
    if not fresh and (row or {}).get("unread"):
        # A buyer repeating the same text past TAIL_BUBBLES looks to the aligner like an
        # already-stored tail (every overlap size matches trivially). The list's unread count is
        # the backstop: unread with nothing fresh means we cannot see what we were told is there.
        _unreadable(
            deps,
            market,
            thread["thread_id"],
            "the list reports unread messages and the tail holds nothing new",
        )
        return None
    for entry in fresh:
        verdict = None
        if entry["direction"] == "in":
            verdict = _scan(deps, thread, entry["text"], stored)["verdict"]
        deps.store.record_inbound(
            thread["thread_id"],
            msg_id=entry["msg_id"],
            text=entry["text"],
            ts=entry["ts"],
            direction=entry["direction"],
            scam_verdict=verdict,
        )
        deps.bus.publish(
            "browser.inbound",
            {
                "market": market,
                "thread_id": thread["thread_id"],
                "dir": entry["direction"],
                "scam_verdict": verdict,
            },
        )
    return len(fresh)


def _scan(deps: InboxDeps, thread: dict, text: str, stored) -> dict:
    """Scan one inbound message as it is written, so the verdict is on the row before any model sees
    it. The engine is deterministic and offline, so this costs nothing."""
    history = "\n".join(row["text"] for row in stored if row["dir"] == "in")
    merged, registry_ok = deps.store.merged_scam_signatures()
    return scam_engine.scan(
        text,
        history_text=history,
        allowlist=hosts.build_allowlist(marketplaces.all_marketplaces()),
        signatures=merged,
        checkout_base=deps.config.carousell_ai_api_base.rstrip("/") + "/checkout",
        registry_ok=registry_ok,
    )


# How long the lane waits after a reply pass that sent nothing. The pacing pre-gate predicts the
# refusals we know about; this is the backstop for the ones nobody has diagnosed yet, and it is a
# flat cooldown rather than exponential backoff on purpose — the job is to turn a 28-second respawn
# loop into a 5-minute one, which is cheap enough to wait out and slow enough to notice, without
# inventing per-thread retry state the eligibility rows deliberately do not carry.
NO_SEND_COOLDOWN_SEC = 300.0


def paced_out_markets(store, config, now=None) -> tuple:
    """The marketplaces whose next buyer reply the pacing engine would refuse right now.

    Asked before a pass is spawned rather than discovered inside one. `send_reply` documents that a
    blocked verdict records nothing and "the thread simply stays unanswered and the reply lane picks
    it up again once `retry_after_sec` has passed" — but eligibility is only ever cleared by a
    *committed* reply, so nothing honoured that delay and the lane respawned the pass immediately.
    On 2026-08-29 one buyer's "still available?" cost 101 model spawns in 48 minutes and was still
    unanswered; the same shape burned 49 on 2026-08-27 against the hourly cap.

    Per market, because the cap is a per-marketplace-account ledger. `peek_action` records nothing,
    so asking never spends the slot the real send needs.
    """
    cfg = pacing_engine.resolve(config, settings.quiet_window_minutes(store))
    now = time.time() if now is None else now
    waiting = {row["market"] for row in store.threads_with_unhandled_inbound()}
    return tuple(
        sorted(
            market
            for market in waiting
            if store.peek_action(marketplace=market, kind="reply", cfg=cfg, now=now)["verdict"]
            != "go"
        )
    )


def in_no_send_cooldown(store, now) -> bool:
    """Whether the last reply pass sent nothing recently enough to hold this tick.

    Read off the ledger rather than kept in lane state: a daemon restart mid-loop must not clear the
    brake, and the pass rows are already the durable record of what happened.
    """
    last = store.last_finished_pass("reply")
    if last is None or last["class"] != "no_send":
        return False
    return (now - last["finished_ts"]) < NO_SEND_COOLDOWN_SEC


def reply_lane(*, store, bus, config, now=None) -> None:
    """One tick of the reply lane: spawn a scoped reply pass for the buyers who are waiting.

    Coalescing and single-flight like the channel lane: the store claims every waiting thread into
    one pass and refuses to enqueue a second while one is in flight, so a burst of buyer messages
    becomes one pass rather than a queue of them. Nothing is auto-refired: a pass that failed
    leaves its threads eligible, and the next tick picks them up because eligibility comes from the
    rows and not from a retry counter.

    That last property is what makes the pacing pre-gate load-bearing rather than an optimization:
    "eligible" and "sendable" are different questions, and a tick that spawns a pass for a thread
    the send path will refuse re-asks the same refused question every ~28 seconds, forever. The gate
    holds the *pass*, never the buyer — the rows stay eligible and the next unblocked tick claims
    them.
    """
    if store.is_paused():
        return
    now = time.time() if now is None else now
    if in_no_send_cooldown(store, now):
        return
    claimed = store.enqueue_reply_pass(skip_markets=paced_out_markets(store, config, now))
    if claimed is None:
        return
    bus.publish(
        "pass.queued",
        {"type": "reply", "threads": len(claimed["thread_ids"])},
        pass_id=claimed["pass_id"],
    )


def _count_blind(
    deps: InboxDeps,
    market: str,
    reason: str,
    *,
    cause: str = blindness.CAUSE_MARKET,
    count: int = 0,
) -> None:
    """Count a failed read, and raise one notice once a run of them means we are genuinely blind.

    `cause` decides which sentence the seller gets, and it is the caller's to supply because only
    the caller knows how far the read got. The rule the causes encode: never tell the seller to
    check something we have already established is fine.
    """
    failures = deps.blind.get(market, 0) + 1
    deps.blind[market] = failures
    deps.blind_since.setdefault(market, deps.now())
    deps.bus.publish(
        "browser.blind",
        {"market": market, "failures": failures, "cause": cause, "reason": reason[:200]},
    )
    if failures >= int(deps.config.browser_blind_after):
        _notify_once(
            deps,
            f"blind:{market}",
            blindness.notice_for(cause, name=marketplaces.display_name(market), count=count),
        )


def _clear_blind(deps: InboxDeps, market: str, *, read_content: bool = False) -> None:
    """A market is seen again. Tell the seller only if they were told it was not.

    `read_content` is the difference between "the conversation list answered" and "I read what
    buyers said", and the recovery notice is gated on the second. A tick that lists fifty
    conversations and cannot open one of them is not a market we can see — announcing it as one
    would invert the lane's first rule, and the seller would be told reading had resumed on the very
    tick that recorded nothing.
    """
    was_blind = deps.blind.pop(market, None)
    since = deps.blind_since.pop(market, None)
    told = bool(deps.notified.get(f"blind:{market}"))
    _clear_notice(deps, f"blind:{market}")
    if not (was_blind and told and read_content and since is not None):
        return
    gap = deps.now() - since
    if gap < blindness.GAP_WORTH_MENTIONING_SEC:
        # Fixed before it could have mattered. The seller was warned, but chasing that with an
        # all-clear minutes later is two messages about a blip they never noticed.
        return
    deps.store.queue_notice(
        blindness.READING_AGAIN_NOTICE.format(
            name=marketplaces.display_name(market), how_long=blindness.gap_text(gap)
        )
    )
    deps.bus.publish("browser.reading_again", {"market": market, "blind_sec": round(gap)})
