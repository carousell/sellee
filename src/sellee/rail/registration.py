"""The registration lane: mail to the seller's registration address, routed by its sender's
registered domain to that marketplace's matcher. Handled mail ids are kept, so none acts twice."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from sellee import marketplaces
from sellee.engines import hosts
from sellee.engines import scam as scam_engine
from sellee.rail.client import RailAuthError, RailError, RailNetworkError, RailUnprovisioned
from sellee.rail.registration_sink import is_registration_refusal, send_registration_reply

log = logging.getLogger(__name__)

CRAIGSLIST = marketplaces.CRAIGSLIST
PAGE_SIZE = 50
# How long a send is left to the call that made it, which retries for under a minute itself.
RETRY_SEND_AFTER_SEC = 120.0

# Craigslist relays each buyer from an address of their own on this host.
_CRAIGSLIST_BUYER_HOST = "reply.craigslist.org"
# The footer Craigslist appends to every relayed buyer mail; a buyer can type one above it.
_CRAIGSLIST_FOOTER = re.compile(r"^Original craigslist post:[ \t]*(?:\r?\n[ \t]*(\S*))?", re.M)

# A marketplace's own mail (sign-up, posting links), handed to its adapter: market -> callback.
SERVICE_HOOKS: dict[str, Callable[[dict], None]] = {}


@dataclass
class RegistrationDeps:
    store: object
    bus: object
    config: object
    rail_factory: Callable
    service_hooks: dict = field(default_factory=lambda: SERVICE_HOOKS)
    now: Callable[[], float] = time.time


def registration_lane(deps: RegistrationDeps) -> None:
    """One tick. An unreachable rail logs and waits for the next tick."""
    try:
        rail = deps.rail_factory()
        items = deps.store.list_items()
        cursor = deps.store.get_registration_cursor()
        while True:
            page = rail.list_registration_mail(cursor, PAGE_SIZE)
            for mail in page["mail"]:
                if not deps.store.registration_mail_seen(mail["id"]):
                    thread_id = _route(deps, mail, items)
                    deps.store.mark_registration_mail_seen(
                        mail["id"],
                        thread_id=thread_id,
                        subject=mail.get("subject") or "",
                        received_ts=_epoch(mail["received_at"]),
                    )
            next_cursor = page["next_cursor"] or cursor
            if next_cursor != cursor:
                deps.store.set_registration_cursor(next_cursor)
            if len(page["mail"]) < PAGE_SIZE or next_cursor == cursor:
                break
            cursor = next_cursor
        _finish_our_sends(deps, rail)
    except RailUnprovisioned:
        return
    except (RailNetworkError, RailAuthError) as exc:
        log.warning("registration read skipped this tick: %s", exc)


def _route(deps: RegistrationDeps, mail: dict, items) -> str | None:
    """Act on one mail; returns the thread it joined, or None."""
    domain = mail.get("from_domain") or ""
    market = marketplaces.market_for_domain(domain)
    matcher = _MATCHERS.get(market or "")
    if matcher is None:
        log.info("registration mail %s from %r is no marketplace's", mail["id"], domain)
        return None
    return matcher(deps, mail, items)


def _match_craigslist(deps: RegistrationDeps, mail: dict, items) -> str | None:
    """A buyer relayed about one of our posts joins a thread; Craigslist's own mail goes to its
    adapter; a relayed buyer about any other post is dropped."""
    sender = (mail.get("from_email") or "").lower()
    if sender.rsplit("@", 1)[-1] != _CRAIGSLIST_BUYER_HOST:
        hook = deps.service_hooks.get(CRAIGSLIST)
        if hook is None:
            log.info("Craigslist mail %s has no adapter to take it; dropped", mail["id"])
            return None
        try:
            hook(mail)
        except Exception:
            # The adapter's fault must not hold back the buyers behind this mail.
            log.exception("the Craigslist adapter failed on mail %s; dropped", mail["id"])
        return None
    item_id = _item_for(items, last_footer_url(mail.get("text") or ""))
    if item_id is None:
        log.info("Craigslist buyer mail %s is about a post we do not manage", mail["id"])
        return None
    return _append_buyer_mail(deps, mail, sender, item_id)


_MATCHERS = {CRAIGSLIST: _match_craigslist}


def last_footer_url(text: str) -> str:
    """The post URL in the last Craigslist footer of a relayed mail, or ""."""
    found = _CRAIGSLIST_FOOTER.findall(text)
    return found[-1] if found else ""


def _append_buyer_mail(deps: RegistrationDeps, mail: dict, sender: str, item_id: str) -> str:
    thread_id = f"{CRAIGSLIST}:{sender}"
    if deps.store.get_thread(thread_id, message_cap=0) is None:
        deps.store.create_thread(
            thread_id=thread_id,
            side="sell",
            market=CRAIGSLIST,
            counterpart_handle=sender,
            item_id=item_id,
            source="registration_read",
        )
        deps.bus.publish(
            "registration.thread_new",
            {"market": CRAIGSLIST, "thread_id": thread_id, "item_id": item_id},
        )
    thread = deps.store.get_thread(thread_id, message_cap=0)
    answered_ts = thread["cursor_last_ts"] if thread else None
    stored = deps.store.get_thread_messages(thread_id)
    inbound = [r for r in stored if r["dir"] == "in" and r["msg_id"] != mail["id"]]
    ts = _epoch(mail["received_at"])
    deps.store.append_thread_message(
        thread_id,
        msg_id=mail["id"],
        direction="in",
        text=mail["text"],
        ts=ts,
        source="marketplace",
        scam_verdict=_scan(deps, mail["text"], [r["text"] for r in inbound])["verdict"],
    )
    owed = any(answered_ts is None or r["ts"] > answered_ts for r in inbound)
    if mail.get("automatic") and not owed:
        # An out-of-office is kept but never answered: the reply cursor moves over it.
        deps.store.mark_relay_answered(thread_id, mail["id"], ts)
    return thread_id


def _finish_our_sends(deps: RegistrationDeps, rail) -> None:
    """Retry our unsettled Craigslist sends under their own id, which bazaar sends once. Its
    answer settles each: success commits it, and a refusal means it was never sent."""
    if deps.store.is_paused():
        return
    cutoff = deps.now() - RETRY_SEND_AFTER_SEC
    for intent in deps.store.unsettled_intents_on(CRAIGSLIST, created_before=cutoff):
        thread = deps.store.get_thread(intent["thread_id"], message_cap=0)
        try:
            send_registration_reply(rail, deps.store, thread, intent["text"], intent["intent_id"])
        except (RailNetworkError, RailAuthError):
            raise
        except RailError as exc:
            if is_registration_refusal(exc):
                log.info("registration send %s refused on retry: %s", intent["intent_id"], exc)
                deps.store.drop_refused_intent(intent["intent_id"])
            continue
        deps.store.settle_intent_from_read(intent["intent_id"])


def _item_for(items, post_url: str) -> str | None:
    """The one item whose Craigslist listing is this post, or None."""
    matches = [
        item["id"]
        for item in items
        if post_url and (item.get("listing_urls") or {}).get(CRAIGSLIST) == post_url
    ]
    return matches[0] if len(matches) == 1 else None


def _epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def _scan(deps: RegistrationDeps, text: str, history: list) -> dict:
    """Scan a buyer message as it is written, as the relay lane does."""
    merged, registry_ok = deps.store.merged_scam_signatures()
    return scam_engine.scan(
        text,
        history_text="\n".join(history),
        allowlist=hosts.build_allowlist(marketplaces.all_marketplaces()),
        signatures=merged,
        checkout_base=deps.config.carousell_ai_web_base_url.rstrip("/") + "/checkout",
        registry_ok=registry_ok,
    )
