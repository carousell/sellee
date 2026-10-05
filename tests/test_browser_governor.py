"""The page-load governor: how fast the agent may move through a market that polices automation.

What an account-integrity model can see is the loads: when they come and how many. The promises
here are about both — a person's gap before every load, never under the floor, and a ceiling per
hour and per day — and about scope: a market that does not police automation is never slowed.
"""

from __future__ import annotations

import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sellee.browser import governor
from sellee.config import Config

_FB = "https://www.facebook.com/messages/t/99/"
_CAROUSELL = "https://www.carousell.sg/inbox/"


class _Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t
        self.slept: list = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def _governor(clock=None, seed=1, **config):
    clock = clock or _Clock()
    return (
        governor.PageGovernor(
            governor.Allowance.from_config(Config(**config)),
            now=clock.now,
            sleep=clock.sleep,
            rng=random.Random(seed),
        ),
        clock,
    )


# --- scope --------------------------------------------------------------------------------------


def test_facebook_is_governed_and_carousell_is_not() -> None:
    gov, _ = _governor()

    assert gov.market_for(_FB) == "fb"
    assert gov.market_for(_CAROUSELL) is None
    assert gov.market_for("about:blank") is None


def test_craigslists_account_pages_are_governed_as_well_as_its_site() -> None:
    gov, _ = _governor()

    assert gov.market_for("https://accounts.craigslist.org/login") == "craigslist"
    assert gov.market_for("https://accounts.craigslist.org/pass?key=1") == "craigslist"
    assert gov.market_for("https://www.craigslist.org/") == "craigslist"


def test_a_market_that_does_not_police_automation_is_never_slowed() -> None:
    gov, clock = _governor(policed_pages_per_hour=1)

    for _ in range(20):
        gov.before_load(_CAROUSELL)

    assert clock.slept == []


# --- the gap ------------------------------------------------------------------------------------


def test_the_first_load_does_not_wait() -> None:
    gov, clock = _governor()

    gov.before_load(_FB)

    assert clock.slept == []


def test_the_next_load_waits_a_persons_gap() -> None:
    gov, clock = _governor()

    gov.before_load(_FB)
    gov.before_load(_FB)

    assert len(clock.slept) == 1
    assert clock.slept[0] >= governor.GAP_FLOOR_SEC


def test_the_gap_is_measured_from_the_last_load() -> None:
    """A load that comes long after the last one has already had its gap."""
    gov, clock = _governor()

    gov.before_load(_FB)
    clock.t += 3600.0
    gov.before_load(_FB)

    assert clock.slept == []


@given(st.integers(min_value=0, max_value=2**32))
def test_no_gap_is_ever_under_the_floor(seed) -> None:
    gov, clock = _governor(seed=seed)

    for _ in range(12):
        gov.before_load(_FB)

    assert all(s >= governor.GAP_FLOOR_SEC for s in clock.slept)


def test_the_gaps_are_not_a_constant_to_measure() -> None:
    gov, clock = _governor(seed=5)

    for _ in range(12):
        gov.before_load(_FB)

    assert len(set(round(s, 3) for s in clock.slept)) > 5


# --- the caps -----------------------------------------------------------------------------------


def test_a_flow_that_would_run_past_the_hour_is_turned_away_before_it_starts() -> None:
    gov, _ = _governor(policed_pages_per_hour=5)

    for _ in range(3):
        gov.before_load(_FB)

    assert gov.can_start("fb", 2)
    assert not gov.can_start("fb", 3)


def test_a_load_past_the_hour_raises_before_anything_is_loaded() -> None:
    gov, _ = _governor(policed_pages_per_hour=2)
    gov.before_load(_FB)
    gov.before_load(_FB)

    with pytest.raises(governor.PagesSpent):
        gov.before_load(_FB)


def test_the_hour_rolls() -> None:
    gov, clock = _governor(policed_pages_per_hour=2)
    gov.before_load(_FB)
    gov.before_load(_FB)

    clock.t += governor.HOUR_SEC + 1

    assert gov.can_start("fb", 2)
    gov.before_load(_FB)


def test_the_day_caps_what_the_hours_would_allow() -> None:
    gov, clock = _governor(policed_pages_per_hour=10, policed_pages_per_day=12)

    for _ in range(10):
        gov.before_load(_FB)
    clock.t += governor.HOUR_SEC + 1
    gov.before_load(_FB)
    gov.before_load(_FB)

    assert not gov.can_start("fb", 1)
    with pytest.raises(governor.PagesSpent):
        gov.before_load(_FB)

    clock.t += governor.DAY_SEC
    assert gov.can_start("fb", 1)


def test_an_ungoverned_market_always_has_room() -> None:
    gov, _ = _governor(policed_pages_per_hour=1)

    assert gov.can_start("carousell", 100)


def test_pages_spent_is_a_browser_error_so_every_caller_already_fails_closed() -> None:
    """The send, the publish and the edit all turn a `BrowserError` before their commit into
    "nothing happened", which is exactly what a refused load is."""
    from sellee.browser.client import BrowserError

    assert issubclass(governor.PagesSpent, BrowserError)


# --- work nobody asked for ------------------------------------------------------------------------


def test_a_lane_without_a_governor_is_never_held() -> None:
    assert governor.has_room(None, "fb", 1000)


def _at(hour: int, minute: int = 0) -> float:
    import time as _time

    base = _time.mktime((2026, 10, 1, hour, minute, 0, 0, 0, -1))
    return float(base)


def test_unprompted_facebook_work_waits_out_the_quiet_hours(store) -> None:
    from tests.conftest import seed_setting

    seed_setting(store, "quiet_hours", [2300, 800])

    assert governor.unprompted_held(store, Config(), "fb", _at(3))
    assert not governor.unprompted_held(store, Config(), "fb", _at(14))


def test_a_market_that_does_not_police_automation_is_never_held_for_quiet_hours(store) -> None:
    from tests.conftest import seed_setting

    seed_setting(store, "quiet_hours", [2300, 800])

    assert not governor.unprompted_held(store, Config(), "carousell", _at(3))
