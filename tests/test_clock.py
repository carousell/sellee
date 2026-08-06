"""The clock check: does this process's day line up with the seller's?

The bug this exists for is silent. Quiet hours are evaluated against the local wall clock, so a
process running eight hours off does not fail — it applies "don't do this at night" to the wrong
eight hours and publishes at four in the morning looking like it worked.
"""

from __future__ import annotations

import time

import pytest

from selly_agent import clock, healthcheck
from selly_agent.installer import checks

# +08:00 all year (no DST), which makes an offset assertion stable whenever the suite runs.
SINGAPORE = "Asia/Singapore"
NOW = 1770000000.0


@pytest.fixture
def utc_clock(monkeypatch):
    """A process whose own clock is UTC — a container's default, and the mismatch that matters."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def singapore_clock(monkeypatch):
    monkeypatch.setenv("TZ", SINGAPORE)
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


# --- the comparison ---------------------------------------------------------------------------


def test_a_utc_process_is_eight_hours_behind_a_singapore_seller(utc_clock) -> None:
    assert clock.offset_delta(SINGAPORE, NOW) == -480


def test_a_matching_clock_reports_no_difference(singapore_clock) -> None:
    assert clock.offset_delta(SINGAPORE, NOW) == 0


def test_an_unrecorded_or_unknown_zone_is_not_a_mismatch(utc_clock) -> None:
    """Unanswerable is a distinct outcome from wrong, and must never be reported as one."""
    assert clock.offset_delta("", NOW) is None
    assert clock.offset_delta("Mars/Olympus_Mons", NOW) is None


def test_the_difference_is_rendered_the_way_a_person_would_say_it() -> None:
    assert clock.render_delta(-480) == "8 hours behind"
    assert clock.render_delta(60) == "1 hour ahead of"
    assert clock.render_delta(-30) == "30 minutes behind"


# --- what the seller hears ----------------------------------------------------------------------


def _notices(store):
    return [n["text"] for n in store.claim_queued_notices(10)]


def test_a_mismatch_is_said_once_with_the_fix(store, bus, utc_clock) -> None:
    store.set_seller_config_section("basics", {"region": "SG", "timezone": SINGAPORE})
    assert clock.check_timezone(store=store, bus=bus, now=NOW) is True
    notices = _notices(store)
    assert len(notices) == 1
    assert "8 hours behind" in notices[0]
    assert f"TZ={SINGAPORE}" in notices[0]
    assert [ev for ev in bus.store.read() if ev.kind == "clock.timezone_mismatch"]


def test_a_clock_that_agrees_says_nothing(store, bus, singapore_clock) -> None:
    store.set_seller_config_section("basics", {"region": "SG", "timezone": SINGAPORE})
    assert clock.check_timezone(store=store, bus=bus, now=NOW) is False
    assert _notices(store) == []


def test_no_recorded_timezone_says_nothing(store, bus, utc_clock) -> None:
    """Setup has not run yet — there is nothing to disagree with."""
    assert clock.check_timezone(store=store, bus=bus, now=NOW) is False
    assert _notices(store) == []


# --- the healthcheck line ---------------------------------------------------------------


def test_the_clock_line_is_a_failure_only_when_the_two_disagree() -> None:
    agreed = healthcheck.clock_check(zone="Asia/Singapore", delta_minutes=0)
    assert agreed.status == checks.OK

    off = healthcheck.clock_check(zone="Asia/Singapore", delta_minutes=-480)
    assert off.status == checks.FAIL
    assert "8 hours behind" in off.detail
    assert "TZ=Asia/Singapore" in off.fix

    # Unanswerable is not the same as wrong.
    assert healthcheck.clock_check(zone="", delta_minutes=None).status == checks.WARN
    unknown = healthcheck.clock_check(zone="Mars/Olympus_Mons", delta_minutes=None)
    assert unknown.status == checks.WARN


def test_the_clock_is_asked_about_only_where_it_can_be_wrong(xdg_tmp, monkeypatch) -> None:
    """On a host the daemon's clock is the seller's clock, so there is no question to ask."""
    from selly_agent import deployment

    assert [result.name for result in healthcheck.run_checks()] == [
        "daemon",
        "channel",
        "browser",
        "browser server",
        "harness",
        "carousell.ai key",
    ]

    monkeypatch.setenv(deployment.MARKER_VAR, deployment.CONTAINER)
    assert [result.name for result in healthcheck.run_checks()][-1] == "clock"
