"""The relay lane: carousell.ai email threads read through the rail into ordinary sell threads.

Each tick pages list_threads from the stored cursor and reads every thread it names in full. Rows
are deduped on bazaar's message id, so the cursor is stored only after they commit and a repeat
or a crash between the two costs a refetch, never a doubled message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from sellee import marketplaces
from sellee.engines import hosts
from sellee.engines import scam as scam_engine
from sellee.rail.client import RailError, RailUnprovisioned, listing_id_from_url
from sellee.store import StoreError

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
        cursor = deps.store.get_relay_cursor()
        while True:
            page = rail.list_threads(cursor, PAGE_SIZE)
            items = None
            for summary in page["threads"]:
                items = deps.store.list_items() if items is None else items
                _import_thread(deps, rail, summary, items)
            next_cursor = page["next_cursor"] or cursor
            if next_cursor != cursor:
                deps.store.set_relay_cursor(next_cursor)
            if len(page["threads"]) < PAGE_SIZE or next_cursor == cursor:
                return
            cursor = next_cursor
    except RailUnprovisioned:
        return
    except RailError as exc:
        log.warning("relay read skipped this tick: %s", exc)


def _import_thread(deps: RelayDeps, rail, summary: dict, items) -> None:
    thread_id = f"{MARKET}:{summary['id']}"
    thread = deps.store.get_thread(thread_id, message_cap=0)
    if thread is None:
        item_id = _item_for(items, summary.get("listing_id", ""))
        if item_id is None:
            log.info("relay thread %s is about a listing we do not manage", thread_id)
            return
        try:
            thread = deps.store.create_thread(
                thread_id=thread_id,
                side="sell",
                market=MARKET,
                counterpart_handle=summary["thread_email"],
                item_id=item_id,
                source="relay_read",
            )
        except StoreError as exc:
            log.warning("could not create thread %s: %s", thread_id, exc)
            return
        deps.bus.publish(
            "relay.thread_new", {"market": MARKET, "thread_id": thread_id, "item_id": item_id}
        )
    _record_messages(deps, thread_id, rail.get_thread(summary["id"])["messages"])
    if summary.get("buyer_blocked") and thread["status"] == "active":
        deps.store.update_thread(thread_id, {"status": "closed"})


def _record_messages(deps: RelayDeps, thread_id: str, messages: list) -> None:
    stored = deps.store.get_thread_messages(thread_id)
    known = {row["msg_id"] for row in stored}
    last_buyer = None
    for message in messages:
        ts = _epoch(message["sent_at"])
        if message["author"] == "buyer":
            last_buyer = (message["id"], ts)
            if message["id"] not in known:
                verdict = _scan(deps, message["text"], stored)
                deps.store.append_thread_message(
                    thread_id,
                    msg_id=message["id"],
                    direction="in",
                    text=message["text"],
                    ts=ts,
                    source="marketplace",
                    scam_verdict=verdict["verdict"],
                )
            continue
        source = _OWN_AUTHORS.get(message["author"])
        if source is None:
            continue
        deps.store.append_thread_message(
            thread_id,
            msg_id=message["id"],
            direction="out",
            text=message["text"],
            ts=ts,
            source=source,
        )
        # An agent reply answers the buyer only once bazaar has sent it everywhere it owes.
        if source == "agent" and last_buyer and not message.get("pending_send"):
            deps.store.mark_relay_answered(thread_id, *last_buyer)


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


def _scan(deps: RelayDeps, text: str, stored) -> dict:
    """Scan a buyer message as it is written, as the browser read lane does."""
    history = "\n".join(row["text"] for row in stored if row["dir"] == "in")
    merged, registry_ok = deps.store.merged_scam_signatures()
    return scam_engine.scan(
        text,
        history_text=history,
        allowlist=hosts.build_allowlist(marketplaces.all_marketplaces()),
        signatures=merged,
        checkout_base=deps.config.carousell_ai_web_base_url.rstrip("/") + "/checkout",
        registry_ok=registry_ok,
    )
