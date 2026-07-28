"""The inbox read lane: see what buyers said, without spending a single token on it.

Each tick the lane asks the marketplace which conversations exist, opens the ones that look like
they moved, reads each one's messages, and reconciles them against the rows already stored. It costs
a navigate and one JS evaluate per thread and no model turns at all, which is what lets the reply
loop above it stay browser-free: by the time a pass runs, what the buyer said is already state.

The conversation list is the marketplace's own API where it has one, and the message read is DOM
work. That split is deliberate — identity, the counterpart, the listing and the unread count are
facts we want typed and loudly wrong when they change; message bubbles are only ever on a page.

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

from selly_agent import marketplaces
from selly_agent.browser import markets as market_adapters
from selly_agent.browser import reconcile
from selly_agent.browser.client import BrowserError, BrowserUnavailable
from selly_agent.engines import hosts
from selly_agent.engines import scam as scam_engine
from selly_agent.store import StoreError

log = logging.getLogger(__name__)

# Statuses whose threads are still worth opening. A held or escalated thread is waiting on someone
# else, and a terminal one is over; reading them would only mark them read on the platform.
_ACTIVE_STATUSES = ("active", "liaising", "agreed")

# Pass types that drive the browser, and so must not overlap the lane's reads.
_BROWSER_PASS_TYPES = ("reply", "publish")

BLIND_NOTICE = (
    "I can't read your {market} inbox right now, so I may be missing buyer messages. "
    "Check that the agent's Chrome is running and still logged in."
)
LOGGED_OUT_NOTICE = (
    "Your {market} session is logged out, so I've stopped reading that market. "
    "Log back in in the agent's Chrome and I'll pick up where I left off."
)
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
    now: Callable[[], float] = time.time


def seller_region(store) -> str | None:
    basics = store.get_seller_config_section("basics") or {}
    region = basics.get("region")
    return str(region) if region else None


def _notify_once(deps: InboxDeps, key: str, text: str) -> None:
    """Queue a needs-me notice at most once per condition, so a lane that keeps failing keeps
    telling the event log and stops telling the seller."""
    if deps.notified.get(key):
        return
    deps.notified[key] = True
    deps.store.queue_notice(text)


def _clear_notice(deps: InboxDeps, key: str) -> None:
    deps.notified.pop(key, None)


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
        deps.bus.publish("browser.unavailable", {"reason": str(exc)})
        _notify_once(deps, "unavailable", UNAVAILABLE_NOTICE.format(reason=exc))
        return
    _clear_notice(deps, "unavailable")

    region = seller_region(deps.store)
    for market in marketplaces.browser_markets():
        adapter = market_adapters.get_adapter(market)
        if adapter is None:
            continue  # a registry entry with no adapter yet is not a market we can read
        try:
            with client.exclusive():
                _read_market(deps, client, adapter, region)
        except BrowserError as exc:
            _count_blind(deps, market, str(exc))


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
        _notify_once(deps, f"logged_out:{market}", LOGGED_OUT_NOTICE.format(market=market))
        return
    _clear_notice(deps, f"logged_out:{market}")

    answer = client.evaluate(adapter.conversations_js)
    if not isinstance(answer, dict) or not isinstance(answer.get("conversations"), list):
        # The list came back as a failure rather than as content. Unlike a DOM read that finds
        # nothing, this cannot be mistaken for an empty inbox, so it is reported as what it is.
        reason = (answer or {}).get("error") if isinstance(answer, dict) else "unreadable"
        _count_blind(deps, market, f"conversation list unavailable: {reason}")
        return
    rows = answer["conversations"]

    tick = deps.ticks.get(market, 0) + 1
    deps.ticks[market] = tick
    full_sweep = tick % max(1, int(deps.config.inbox_full_sweep_every)) == 0

    known = {t["thread_id"]: t for t in deps.store.list_threads(side="sell")}
    items = deps.store.list_items()
    opened = 0
    recorded = 0
    unreadable = 0
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
        if thread["status"] not in _ACTIVE_STATUSES:
            continue
        if not full_sweep and _can_skip(deps.store, thread, row):
            continue
        opened += 1
        fresh = _read_thread(deps, client, adapter, thread, region)
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
        },
    )
    # Only a read where every opened thread was legible counts as seeing the market. A conversation
    # whose message list we could not find is the same class of failure as an unreadable inbox: it
    # would otherwise pass for "this buyer said nothing new", which is how one gets stranded.
    if unreadable:
        _count_blind(deps, market, f"{unreadable} conversation(s) unreadable")
    else:
        _clear_blind(deps, market)


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


def _adopt(deps: InboxDeps, market: str, adapter, thread_id: str, row: dict, handle: str, items):
    """Create a thread for a buyer writing about one of our listings for the first time.

    Three things have to hold: the buyer approached us (`received` — not an offer we made), the
    conversation names a listing we recognise, and we know who they are. A thread attached to the
    wrong item would negotiate against the wrong floor, so anything less is left alone — most often
    it is simply a listing the seller made outside the agent.
    """
    if row.get("offer_type") not in (None, "", "received"):
        return False
    item_id = reconcile.match_item(row.get("product_id"), items, market, adapter.listing_id_pattern)
    if not item_id or not handle:
        deps.bus.publish(
            "browser.unmatched", {"market": market, "thread_id": thread_id, "reason": "ambiguous"}
        )
        return False
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


def _read_thread(deps: InboxDeps, client, adapter, thread: dict, region: str | None) -> int | None:
    """Open one thread and reconcile its tail. Returns how many rows were new, or None when the
    conversation could not be read at all — which the caller counts as being blind on this market,
    never as the buyer having said nothing."""
    market = thread["market"]
    native = thread["thread_id"].split(":", 1)[1] if ":" in thread["thread_id"] else ""
    url = marketplaces.market_url(market, "thread", region, thread_id=native)
    if url is None:
        log.warning("no recorded thread URL template for %s", market)
        return None
    client.navigate(url)
    raw = client.evaluate(adapter.tail_js)
    if raw is None:
        # The reader could not find the message list. An empty tail would claim the conversation is
        # over; this says we could not see it.
        return None
    tail = reconcile.classify_tail(raw)
    stored = deps.store.get_thread_messages(thread["thread_id"], limit=None)
    if not tail:
        # A thread exists because somebody wrote in it, so a conversation we have already recorded
        # messages for cannot legitimately read as empty. If it does, the page changed shape under
        # the reader — report that rather than let it pass for "the buyer said nothing new".
        return None if stored else 0
    fresh = reconcile.new_rows(tail, stored, now=deps.now())
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
    it. The engine is deterministic and offline, so this costs nothing and cannot be argued with."""
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


def reply_lane(*, store, bus) -> None:
    """One tick of the reply lane: spawn a scoped reply pass for the buyers who are waiting.

    Coalescing and single-flight like the channel lane: the store claims every waiting thread into
    one pass and refuses to enqueue a second while one is in flight, so a burst of buyer messages
    becomes one pass rather than a queue of them. Nothing is auto-refired: a pass that failed
    leaves its threads eligible, and the next tick picks them up because eligibility comes from the
    rows and not from a retry counter.
    """
    if store.is_paused():
        return
    claimed = store.enqueue_reply_pass()
    if claimed is None:
        return
    bus.publish(
        "pass.queued",
        {"type": "reply", "threads": len(claimed["thread_ids"])},
        pass_id=claimed["pass_id"],
    )


def _count_blind(deps: InboxDeps, market: str, reason: str) -> None:
    """Count a failed read, and raise one notice once a run of them means we are genuinely blind."""
    failures = deps.blind.get(market, 0) + 1
    deps.blind[market] = failures
    deps.bus.publish(
        "browser.blind", {"market": market, "failures": failures, "reason": reason[:200]}
    )
    if failures >= int(deps.config.browser_blind_after):
        _notify_once(deps, f"blind:{market}", BLIND_NOTICE.format(market=market))


def _clear_blind(deps: InboxDeps, market: str) -> None:
    deps.blind.pop(market, None)
    _clear_notice(deps, f"blind:{market}")
