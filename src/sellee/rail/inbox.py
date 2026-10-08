"""The relay lane: carousell.ai email threads read through the rail into ordinary sell threads.

Each tick pages list_threads from the stored cursor and reads every thread it names in full. Rows
are deduped on bazaar's message id, so the cursor is stored only after they commit and a repeat
or a crash between the two costs a refetch, never a doubled message.

bazaar lists a thread again only when a message lands or its buyer is blocked, so a thread with
something still unsettled is kept in relay_rereads and read on every tick until it settles.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from sellee import marketplaces
from sellee.engines import hosts
from sellee.engines import scam as scam_engine
from sellee.rail.client import (
    RailAuthError,
    RailNetworkError,
    RailUnprovisioned,
    listing_id_from_url,
)

log = logging.getLogger(__name__)

MARKET = marketplaces.RAIL
PAGE_SIZE = 50
# Our side of the conversation: the seller from their inbox, or an agent's reply.
_OWN_AUTHORS = {"seller": "manual", "agent": "agent"}


@dataclass
class RelayDeps:
    store: object
    bus: object
    config: object
    rail_factory: Callable


def relay_lane(deps: RelayDeps) -> None:
    """One tick. An unreachable rail logs and waits for the next tick."""
    try:
        rail = deps.rail_factory()
        items = deps.store.list_items()
        cursor = deps.store.get_relay_cursor()
        while True:
            page = rail.list_threads(cursor, PAGE_SIZE)
            for summary in page["threads"]:
                _import(deps, rail, summary, items)
            next_cursor = page["next_cursor"] or cursor
            if next_cursor != cursor:
                deps.store.set_relay_cursor(next_cursor)
            if len(page["threads"]) < PAGE_SIZE or next_cursor == cursor:
                break
            cursor = next_cursor
        for pending in deps.store.relay_rereads():
            # An unplaced thread costs a call only once an item has its listing.
            if pending["placed"] or _item_for(items, pending["listing_id"]):
                _import(deps, rail, None, items, bazaar_id=pending["id"])
    except RailUnprovisioned:
        return
    except (RailNetworkError, RailAuthError) as exc:
        log.warning("relay read skipped this tick: %s", exc)


def _import(deps: RelayDeps, rail, summary: dict | None, items, *, bazaar_id: str = "") -> None:
    """Import one thread, and keep it for the next tick while anything about it is unsettled."""
    bazaar_id = bazaar_id or summary["id"]  # type: ignore[index]
    listing_id = (summary or {}).get("listing_id", "")
    try:
        full = rail.get_thread(bazaar_id)
        listing_id = full["thread"].get("listing_id", "") or listing_id
        placed, settled = _import_thread(deps, full["thread"], full["messages"], items)
    except (RailNetworkError, RailAuthError):
        raise
    except Exception:
        # One thread bazaar cannot serve, or we cannot store, must not hold back the rest.
        log.exception("relay thread %s could not be read; trying again next tick", bazaar_id)
        placed, settled = True, False
    if settled:
        deps.store.drop_relay_reread(bazaar_id)
    else:
        deps.store.keep_relay_reread(bazaar_id, listing_id, placed=placed)


def _import_thread(deps: RelayDeps, summary: dict, messages: list, items) -> tuple[bool, bool]:
    """Write one thread; returns (placed on an item, nothing left to do)."""
    thread_id = f"{MARKET}:{summary['id']}"
    thread = deps.store.get_thread(thread_id, message_cap=0)
    if thread is None:
        item_id = _item_for(items, summary.get("listing_id", ""))
        if item_id is None:
            log.info("relay thread %s is about a listing we do not manage", thread_id)
            return False, False
        thread = deps.store.create_thread(
            thread_id=thread_id,
            side="sell",
            market=MARKET,
            counterpart_handle=summary["thread_email"],
            item_id=item_id,
            source="relay_read",
        )
        deps.bus.publish(
            "relay.thread_new", {"market": MARKET, "thread_id": thread_id, "item_id": item_id}
        )
    owed = _record_messages(deps, thread_id, messages)
    if not summary.get("buyer_blocked"):
        return True, not owed
    return True, deps.store.close_blocked_relay_thread(thread_id)


def _record_messages(deps: RelayDeps, thread_id: str, messages: list) -> bool:
    """Write the messages not yet stored. True while an agent reply is still owed a send."""
    stored = deps.store.get_thread_messages(thread_id)
    known = {row["msg_id"] for row in stored}
    history = [row["text"] for row in stored if row["dir"] == "in"]
    last_buyer, owed = None, False
    for message in messages:
        ts = _epoch(message["sent_at"])
        author = message["author"]
        if author == "buyer":
            last_buyer = (message["id"], ts)
        if message["id"] not in known and (author == "buyer" or author in _OWN_AUTHORS):
            inbound = author == "buyer"
            deps.store.append_thread_message(
                thread_id,
                msg_id=message["id"],
                direction="in" if inbound else "out",
                text=message["text"],
                ts=ts,
                source="marketplace" if inbound else _OWN_AUTHORS[author],
                scam_verdict=_scan(deps, message["text"], history)["verdict"] if inbound else None,
            )
            if inbound:
                history.append(message["text"])
        if author == "agent":
            owed = owed or bool(message.get("pending_send"))
            # An agent reply answers the buyer only once bazaar has sent it everywhere it owes.
            if last_buyer and not owed:
                answered_id, answered_ts = last_buyer
                deps.store.mark_relay_answered(thread_id, answered_id, answered_ts)
    return owed


def _item_for(items, listing_id: str) -> str | None:
    """The one item published to this carousell.ai listing, or None."""
    matches = [
        item["id"]
        for item in items
        if listing_id
        and listing_id_from_url((item.get("listing_urls") or {}).get(MARKET, "")) == listing_id
    ]
    return matches[0] if len(matches) == 1 else None


def _epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def _scan(deps: RelayDeps, text: str, history: list) -> dict:
    """Scan a buyer message as it is written, as the browser read lane does."""
    merged, registry_ok = deps.store.merged_scam_signatures()
    return scam_engine.scan(
        text,
        history_text="\n".join(history),
        allowlist=hosts.build_allowlist(marketplaces.all_marketplaces()),
        signatures=merged,
        checkout_base=deps.config.carousell_ai_web_base_url.rstrip("/") + "/checkout",
        registry_ok=registry_ok,
    )
