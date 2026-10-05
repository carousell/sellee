"""How fast the agent may move through a marketplace that polices automation.

A person moves between pages at a person's pace: they read what loaded, then click. The agent loaded
the next page the moment it had scraped the last — thirty-two Facebook conversations in three
minutes and twenty seconds on 2026-09-29 — and kept that rate all night. Every page load of a
market whose adapter `polices_automation` goes through `before_load`, which `BrowserClient.navigate`
calls, so no lane can go faster than this by being written without it.

Two limits, answering different questions:

  * A gap before each load, drawn per load around a median and never under a floor, measured from
    that market's last load. It is slept, not refused: the next page of a visit is still worth
    loading, only not yet.
  * Rolling allowances per hour and per day. A flow that needs several loads asks `can_start`
    before it begins, so it is turned away whole rather than halfway through a form; a load past an
    allowance anyway raises `PagesSpent` before anything is loaded — the backstop.

The counts are kept in process, like the read lane's counters: a restart forgets them, which errs
toward loading more, and the allowances are sized so one restart's forgetting does not matter.
"""

from __future__ import annotations

import random
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass
from typing import Callable

from sellee import marketplaces, settings
from sellee.browser import markets as market_adapters
from sellee.browser.client import BrowserError
from sellee.config import POLICED_PAGE_GAP_FLOOR_SEC
from sellee.engines import pacing as pacing_engine

HOUR_SEC = 3600.0
DAY_SEC = 86400.0
GAP_FLOOR_SEC = POLICED_PAGE_GAP_FLOOR_SEC
# How far one gap wanders from the median — the sigma of a lognormal around it.
GAP_SPREAD = 0.5


class PagesSpent(BrowserError):
    """A load past a market's hourly or daily allowance. Nothing was loaded.

    A `BrowserError` on purpose: the send, the publish and the edit already treat one raised before
    their commit as "nothing happened", which is exactly what this is.
    """


@dataclass(frozen=True)
class Allowance:
    gap_sec: float
    per_hour: int
    per_day: int

    @classmethod
    def from_config(cls, config) -> Allowance:
        return cls(
            gap_sec=max(float(config.policed_page_gap_sec), GAP_FLOOR_SEC),
            per_hour=int(config.policed_pages_per_hour),
            per_day=int(config.policed_pages_per_day),
        )


def _policed_hosts() -> dict:
    """Every site of every market that polices automation, mapped to the market. Read at use, so a
    registry or adapter change needs no restart."""
    hosts = {}
    for adapter in market_adapters.adapters():
        if not adapter.polices_automation:
            continue
        entry = marketplaces.get_marketplace(adapter.market) or {}
        for host in (entry.get("domains") or {}).values():
            hosts[str(host).lower()] = adapter.market
        for url in (adapter.home_url, adapter.publish_url):
            if url:
                hosts[urllib.parse.urlsplit(url).netloc.lower()] = adapter.market
    return hosts


class PageGovernor:
    """One per daemon, shared by every browser client it makes, so a recycled client cannot reset
    what the market has already been shown."""

    def __init__(
        self,
        allowance: Allowance,
        *,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        rng=random,
    ):
        self._allowance = allowance
        self._now = now
        self._sleep = sleep
        self._rng = rng
        self._lock = threading.RLock()
        self._loads: dict = {}
        self._next_gap: dict = {}

    def market_for(self, url: str) -> str | None:
        """The policed market this URL loads a page of, or None when it is not one."""
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        return _policed_hosts().get(host) if host else None

    def can_start(self, market: str, loads: int = 1) -> bool:
        """Whether `loads` more pages of `market` fit inside both allowances right now. Always True
        for a market that does not police automation."""
        if market not in set(_policed_hosts().values()):
            return True
        with self._lock:
            return self._room(market) >= loads

    def before_load(self, url: str) -> None:
        """Wait out the gap before a load of a policed market, or refuse one past its allowance."""
        market = self.market_for(url)
        if market is None:
            return
        with self._lock:
            if self._room(market) < 1:
                raise PagesSpent(
                    f"{market} has had all the page loads it gets for now; nothing was loaded"
                )
            loads = self._loads.setdefault(market, deque())
            if loads:
                gap = self._next_gap.get(market, GAP_FLOOR_SEC)
                wait = gap - (self._now() - loads[-1])
                if wait > 0:
                    self._sleep(wait)
            loads.append(self._now())
            self._next_gap[market] = self._draw_gap()

    def _room(self, market: str) -> int:
        now = self._now()
        loads = self._loads.get(market, deque())
        while loads and now - loads[0] >= DAY_SEC:
            loads.popleft()
        in_hour = sum(1 for t in loads if now - t < HOUR_SEC)
        return min(self._allowance.per_hour - in_hour, self._allowance.per_day - len(loads))

    def _draw_gap(self) -> float:
        return max(
            GAP_FLOOR_SEC, self._allowance.gap_sec * self._rng.lognormvariate(0.0, GAP_SPREAD)
        )


def has_room(governor, market: str, loads: int = 1) -> bool:
    """Whether a lane may start `loads` page loads of `market` now. A lane built without a governor
    is never held."""
    return governor is None or governor.can_start(market, loads)


def in_quiet_hours(store, config, now: float) -> bool:
    """Whether `now` is inside the seller's quiet window, resolved against the pacing config — so
    fast mode, while it lasts, has none."""
    cfg = pacing_engine.resolve(config, settings.quiet_window_minutes(store), now=now)
    stamp = time.localtime(now)
    return pacing_engine.in_quiet_window(
        stamp.tm_hour * 60 + stamp.tm_min, cfg.quiet_start_min, cfg.quiet_end_min
    )


def unprompted_held(store, config, market: str, now: float) -> bool:
    """Whether work nobody asked for — a survey, an adoption, a fan-out publish, an edit's retry —
    waits on this market until the seller's quiet hours end.

    Only on a market that polices automation, where what gives an account away is starting things
    at 4am. A reply is not held: a buyer who just wrote is awake and waiting, and answering them is
    the most ordinary thing a seller does.
    """
    adapter = market_adapters.get_adapter(market)
    if adapter is None or not adapter.polices_automation:
        return False
    return in_quiet_hours(store, config, now)
