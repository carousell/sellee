"""The window seam: the two policy gates in front of a raise, the setting that drives watch mode,
and the subscriber that brings the window forward when the seller turns watching on.

Everything here is about *not* promising a window the seller will never see: off macOS, or with
Chrome on the seller's own machine rather than this one, the raise cannot happen — and every caller
has to hear that as a quiet False rather than an exception.
"""

from __future__ import annotations

import threading

import pytest

from sellee import settings
from sellee.browser import window
from sellee.config import Config


class _Event:
    def __init__(self, kind, payload):
        self.kind = kind
        self.payload = payload


@pytest.fixture
def raises(monkeypatch):
    """Records every pid-raise attempt, standing in for the macOS activation."""
    calls: list = []
    monkeypatch.setattr(window.foreground, "raise_window", lambda port: calls.append(port) or True)
    monkeypatch.setattr(window.foreground, "is_supported", lambda: True)
    monkeypatch.setattr(window.deployment, "is_container", lambda: False)
    monkeypatch.setattr(window.chrome, "resolve_port", lambda configured: configured or 9222)
    return calls


# --- where the window is, and whether it can be raised at all -------------------------------------


def test_the_container_window_is_named_as_the_sellers_own_machine(monkeypatch) -> None:
    monkeypatch.setattr(window.deployment, "is_container", lambda: True)
    assert window.where() == window.WINDOW_IN_CONTAINER
    assert window.can_raise() is False  # that window belongs to a machine we are not running on


def test_a_host_window_is_named_as_the_one_beside_the_sellers_own_chrome(monkeypatch) -> None:
    monkeypatch.setattr(window.deployment, "is_container", lambda: False)
    assert window.where() == window.WINDOW_HERE


def test_a_platform_that_cannot_activate_never_claims_it_raised(monkeypatch) -> None:
    monkeypatch.setattr(window.deployment, "is_container", lambda: False)
    monkeypatch.setattr(window.foreground, "is_supported", lambda: False)
    assert window.can_raise() is False
    assert window.raise_now(9222) is False


def test_an_activation_that_throws_is_a_false_not_a_crash(monkeypatch, raises) -> None:
    """No caller of this — a lane, a send, a pass spawn — may fail over a window."""

    def _boom(port):
        raise OSError("no window server")

    monkeypatch.setattr(window.foreground, "raise_window", _boom)
    assert window.raise_now(9222) is False


def test_a_raise_resolves_the_port_the_way_every_other_caller_does(raises) -> None:
    assert window.raise_now(None) is True
    assert raises == [9222]


# --- the gate on the setting ----------------------------------------------------------------------


def test_nothing_is_raised_while_the_seller_is_not_watching(store, raises) -> None:
    assert window.raise_if_watching(Config(), store) is False
    assert raises == []


def test_watching_raises_the_window_for_the_work(store, bus, raises) -> None:
    settings.set_now(store, bus, key=window.WATCH_SETTING, raw_value=True)
    assert window.raise_if_watching(Config(chrome_cdp_port=9333), store) is True
    assert raises == [9333]


# --- the turn-on subscriber -----------------------------------------------------------------------


def _drain() -> None:
    """The subscriber hands the raise to a thread, so the shell-out never stalls a receive loop."""
    for thread in threading.enumerate():
        if thread.name == "watch-window-raise":
            thread.join(timeout=2.0)


def test_turning_watch_mode_on_brings_the_window_forward_once(raises) -> None:
    window.watch_raiser(Config())(
        _Event("setting.changed", {"key": "watch_browser", "value": True})
    )
    _drain()
    assert raises == [9222]


def test_turning_it_off_raises_nothing(raises) -> None:
    window.watch_raiser(Config())(
        _Event("setting.changed", {"key": "watch_browser", "value": False})
    )
    _drain()
    assert raises == []


@pytest.mark.parametrize(
    "event",
    [
        _Event("setting.changed", {"key": "quiet_hours", "value": [2300, 800]}),
        _Event("escalation.open", {"id": "esc_1"}),
        _Event("setting.changed", {}),
    ],
)
def test_no_other_event_moves_the_window(event, raises) -> None:
    window.watch_raiser(Config())(event)
    _drain()
    assert raises == []


def test_the_subscriber_starts_no_thread_where_a_raise_is_impossible(monkeypatch, raises) -> None:
    """A container publishes the same event; spawning a thread to discover it can do nothing is
    work nobody asked for."""
    monkeypatch.setattr(window.deployment, "is_container", lambda: True)
    before = threading.active_count()
    window.watch_raiser(Config())(
        _Event("setting.changed", {"key": "watch_browser", "value": True})
    )
    assert threading.active_count() == before
    assert raises == []
