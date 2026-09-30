"""The doorbell: hearing a marketplace ring without opening it.

A person does not reload Messenger every five minutes to see whether a buyer wrote; the phone or
the browser pings, and a minute or two later they open the conversation. Facebook was shown the
other thing — its inbox loaded eleven to thirteen times an hour, day and night — and flagged the
account for automation twice. So on a marketplace whose adapter reads on a "notification" trigger,
the inbox is opened only when the marketplace rings.

What rings is a notification the agent's Chrome displayed. It is heard from DevTools'
background-services log (`chrome.recorded_notifications`), which Chrome keeps for itself and
replays on every look, so a short connection each tick hears everything since the last — without
loading, scripting or even attaching to the marketplace's page. `market_rings` makes each ring heard
once, and each is owed a visit a person's reaction time later (`pacing.reaction_delay_sec`). The
read lane pays that visit (`inbox.inbox_lane` on the notification trigger).

Two things are deliberately not rings. What rang while nobody was listening — the first look after
an install or a restart replays days of it — is kept as evidence the doorbell works and asks for no
visit. And pushes that arrive in a burst as the laptop wakes wait until it has been awake a while,
because the network is still coming back and a burst of page loads on wake is its own tell.

The doorbell must never pass for a quiet inbox. Three things say it cannot hear: Chrome will not be
asked, the marketplace's notifications are switched off in Chrome, or days go by without a single
ring from a marketplace that rings several times a day on its own. Each is said once, until it
clears.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable

from sellee import marketplaces, paths, settings
from sellee.browser import chrome, window
from sellee.browser import markets as market_adapters
from sellee.engines import pacing as pacing_engine

log = logging.getLogger(__name__)

# A ring older than this when first heard rang while nobody was listening. Recorded answered.
STALE_RING_SEC = 1800.0
# A gap between ticks this long means the machine slept rather than a tick ran late.
SLEPT_AFTER_SEC = 180.0
# How long the machine must have been awake before a visit a wake-up burst asked for may start.
WAKE_SETTLE_SEC = 300.0
# Looks in a row that could not ask Chrome before the seller is told the doorbell cannot hear.
DEAF_AFTER = 6
# How often the permission is read off Chrome's profile; a file read, not a question to Chrome.
PERMISSION_CHECK_SEC = 600.0
# Facebook rings a few times a day by itself. This long listening with no ring at all is not quiet.
SILENT_AFTER_SEC = 3 * 86400.0
# What Chrome records an allowed site notification permission as.
_ALLOWED = 1

DEAF_NOTICE = (
    "I can't hear {name} ringing right now, so I'm not opening your {name} messages — I only look "
    "when it notifies my Chrome, and my Chrome isn't answering me about its notifications. I'll "
    "pick up again as soon as it does. Until then your {name} app has anything I've missed."
)
NOTIFICATIONS_OFF_NOTICE = (
    "{name} notifications are switched off in my Chrome, so I can't hear {name} ring and I'm not "
    "opening your {name} messages — I only look when it notifies me. To turn them back on, open "
    "{host} in my Chrome window{where} and allow notifications when it asks, or switch browser "
    "notifications on in {name}'s notification settings. Until then your {name} app has anything "
    "I've missed."
)
SILENT_NOTICE = (
    "I haven't heard {name} ring for {days} days. I only open your {name} messages when it "
    "notifies my Chrome, so if those notifications have stopped I could be missing buyers. Check "
    "the {name} app on your phone for anything waiting, and that browser notifications are still "
    "on in {name}'s notification settings."
)


@dataclass
class DoorbellDeps:
    store: object
    bus: object
    config: object
    # The agent's Chrome's CDP port if it is answering, else None. Never launches it, and never goes
    # through the browser factory: that would start the page server and re-assert focus on every
    # tab each tick, which is the opposite of listening quietly.
    port: Callable[[], int | None]
    listen: Callable[[int], list | None] = chrome.recorded_notifications
    now: Callable[[], float] = time.time
    rng: object = random
    # Lane state, in process like the read lane's: a restart re-arming it errs toward telling the
    # seller again.
    last_tick: float | None = None
    settle_until: float = 0.0
    deaf: dict = field(default_factory=dict)
    listening_since: dict = field(default_factory=dict)
    permission_checked: dict = field(default_factory=dict)
    notified: dict = field(default_factory=dict)


def rung_markets(store) -> list:
    """The connected markets whose inbox is read when they ring."""
    rung = []
    for market in settings.connected_markets(store):
        adapter = market_adapters.get_adapter(market)
        if adapter is not None and adapter.read_trigger == "notification":
            rung.append(market)
    return rung


def doorbell_lane(deps: DoorbellDeps) -> None:
    """One tick: hear what rang, and say so if nothing can be heard."""
    if deps.store.is_paused():
        return
    now = deps.now()
    if deps.last_tick is not None and now - deps.last_tick > SLEPT_AFTER_SEC:
        deps.settle_until = now + WAKE_SETTLE_SEC
        deps.bus.publish("doorbell.woke", {"asleep_sec": round(now - deps.last_tick)})
    deps.last_tick = now
    markets = rung_markets(deps.store)
    if not markets:
        return
    port = deps.port()
    heard = deps.listen(port) if port is not None else None
    for market in markets:
        deps.listening_since.setdefault(market, now)
        if heard is None:
            _deaf(deps, market)
            continue
        deps.deaf.pop(market, None)
        deps.notified.pop(f"deaf:{market}", None)
        _hear(deps, market, heard, now)
        _check_permission(deps, market, now)
        _check_silence(deps, market, now)


def _hosts(market: str) -> set:
    entry = marketplaces.get_marketplace(market) or {}
    return {str(host).lower() for host in (entry.get("domains") or {}).values()}


def _hear(deps: DoorbellDeps, market: str, heard: list, now: float) -> None:
    hosts = _hosts(market)
    adapter = market_adapters.get_adapter(market)
    fresh, stale = [], []
    for ring in heard:
        host = (urllib.parse.urlsplit(ring.get("origin") or "").hostname or "").lower()
        if host not in hosts:
            continue
        shown = float(ring["shown_ts"])
        key = f"{ring.get('tag') or ''}|{shown:.3f}"
        kind = adapter.ring_kind(ring.get("title") or "", ring.get("body") or "")
        (stale if now - shown > STALE_RING_SEC else fresh).append((key, kind, shown))
    if stale:
        deps.store.record_rings(market, stale, due_ts=now, now=now, answered=True)
    if not fresh:
        return
    reaction = pacing_engine.reaction_delay_sec(deps.rng, float(deps.config.ring_reaction_sec))
    due = max(now + reaction, deps.settle_until)
    added = deps.store.record_rings(market, fresh, due_ts=due, now=now)
    if added:
        deps.bus.publish(
            "doorbell.rang", {"market": market, "rings": added, "due_in": round(due - now)}
        )


def _deaf(deps: DoorbellDeps, market: str) -> None:
    looks = deps.deaf.get(market, 0) + 1
    deps.deaf[market] = looks
    if looks >= DEAF_AFTER:
        _say_once(
            deps, f"deaf:{market}", DEAF_NOTICE.format(name=marketplaces.display_name(market))
        )


def notifications_allowed(market: str) -> bool | None:
    """Whether Chrome's profile lets this market show notifications, or None when this machine
    cannot read that profile (a container, whose Chrome is on the seller's own desktop)."""
    prefs = paths.browser_profile_dir() / "Default" / "Preferences"
    try:
        data = json.loads(prefs.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    content = (data.get("profile") or {}).get("content_settings") or {}
    rules = (content.get("exceptions") or {}).get("notifications") or {}
    for host in _hosts(market):
        rule = rules.get(f"https://{host}:443,*") or {}
        if rule.get("setting") == _ALLOWED:
            return True
    return False


def _check_permission(deps: DoorbellDeps, market: str, now: float) -> None:
    last = deps.permission_checked.get(market)
    if last is not None and now - last < PERMISSION_CHECK_SEC:
        return
    deps.permission_checked[market] = now
    if notifications_allowed(market) is not False:
        deps.notified.pop(f"off:{market}", None)
        return
    hosts = sorted(_hosts(market))
    _say_once(
        deps,
        f"off:{market}",
        NOTIFICATIONS_OFF_NOTICE.format(
            name=marketplaces.display_name(market),
            host=hosts[0] if hosts else market,
            where=window.where(),
        ),
    )


def _check_silence(deps: DoorbellDeps, market: str, now: float) -> None:
    since = max(deps.store.last_ring_ts(market) or 0.0, deps.listening_since.get(market, now))
    if now - since < SILENT_AFTER_SEC:
        deps.notified.pop(f"silent:{market}", None)
        return
    _say_once(
        deps,
        f"silent:{market}",
        SILENT_NOTICE.format(
            name=marketplaces.display_name(market), days=int((now - since) // 86400)
        ),
    )


def _say_once(deps: DoorbellDeps, key: str, text: str) -> None:
    if deps.notified.get(key):
        return
    deps.notified[key] = True
    deps.store.queue_notice(text)
    deps.bus.publish("doorbell.notice", {"condition": key})
