"""Acquiring the browser means ensuring it runs — the daemon's one acquisition path.

Every actor that needs the browser (the read lane, the reply send, the selector probe, the
fan-out) goes through the factory `make_browser_factory` builds. These tests pin what acquiring
does: Node first, Chrome ensured on every call, the window announced when a launch happened, and
`BrowserUnavailable` carrying the by-hand command when Chrome will not come up.
"""

from __future__ import annotations

import time

import pytest

from sellee import daemon
from sellee.browser import blindness, chrome
from sellee.browser.client import BrowserUnavailable
from sellee.config import Config


def _notices(store):
    return [n["text"] for n in store.claim_queued_notices(10)]


def _launch_events(bus):
    return [ev for ev in bus.store.read() if ev.kind == "browser.chrome_launched"]


# --- ensure_chrome ------------------------------------------------------------------------------


def test_a_ready_chrome_is_acquired_silently(store, bus, monkeypatch) -> None:
    monkeypatch.setattr(daemon.chrome, "ensure_running", lambda port, **kw: (chrome.READY, 9222))
    daemon.ensure_chrome(Config(), store, bus)
    assert _notices(store) == []
    assert _launch_events(bus) == []


def test_a_launched_chrome_is_announced_and_evented(store, bus, monkeypatch) -> None:
    """A window appearing on its own is alarming; the seller hears why before anything drives it.
    The copy names no flow, because any actor may be the one that opens it."""
    monkeypatch.setattr(
        daemon.chrome, "ensure_running", lambda port, **kw: (chrome.LAUNCHED, 45123)
    )
    daemon.ensure_chrome(Config(), store, bus)
    assert _notices(store) == [daemon.CHROME_STARTED_NOTICE]
    assert len(_launch_events(bus)) == 1


def test_a_chrome_that_cannot_start_raises_the_by_hand_command(store, bus, monkeypatch) -> None:
    monkeypatch.setattr(
        daemon.chrome, "ensure_running", lambda port, **kw: (chrome.UNAVAILABLE, None)
    )
    with pytest.raises(BrowserUnavailable) as exc:
        daemon.ensure_chrome(Config(), store, bus)
    assert "--remote-debugging-port" in str(exc.value)
    assert _notices(store) == []


# --- probe-only acquisition (the browser is on the seller's own machine) ------------------------


def test_a_container_never_asks_for_a_launch(store, bus, container, xdg_tmp, monkeypatch) -> None:
    seen = {}

    def _ensure(port, **kwargs):
        seen.update(kwargs, port=port)
        return chrome.READY, port

    monkeypatch.setattr(daemon.chrome, "ensure_running", _ensure)
    daemon.ensure_chrome(Config(), store, bus)
    assert seen["may_launch"] is False
    # The profile Chrome would announce its port into is on the other machine, so the port cannot
    # come out of this call — it has to be a number both sides already agree on.
    assert seen["port"] == chrome.DEFAULT_CDP_PORT


def test_a_closed_host_chrome_points_at_the_script_not_at_an_argv(
    store, bus, container, xdg_tmp, monkeypatch
) -> None:
    """The argv would describe a Chrome on this machine; the one that matters is the seller's,
    with its own profile and its own path to the binary."""
    monkeypatch.setattr(
        daemon.chrome, "ensure_running", lambda port, **kw: (chrome.UNAVAILABLE, None)
    )
    with pytest.raises(BrowserUnavailable) as exc:
        daemon.ensure_chrome(Config(), store, bus)
    message = str(exc.value)
    assert "start-chrome.sh" in message and "start-chrome.ps1" in message
    assert "--remote-debugging-port" not in message
    assert _notices(store) == []


def test_a_probe_only_ensure_answers_ready_when_the_port_answers(monkeypatch) -> None:
    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: True)
    assert chrome.ensure_running(9222, may_launch=False) == (chrome.READY, 9222)


def test_a_probe_only_ensure_starts_nothing_and_touches_nothing(monkeypatch) -> None:
    """The negatives are the point: no binary resolved, no profile lock cleared, nothing spawned.
    Each of those would be acting on this machine's Chrome, which is not the one being asked
    about."""
    touched = []
    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: False)
    monkeypatch.setattr(chrome, "resolve_binary", lambda *a, **kw: touched.append("resolve") or "")
    monkeypatch.setattr(chrome, "clear_stale_locks", lambda: touched.append("locks") or [])
    monkeypatch.setattr(
        chrome.subprocess, "Popen", lambda *a, **kw: touched.append("popen") or None
    )

    assert chrome.ensure_running(9222, may_launch=False) == (chrome.UNAVAILABLE, None)
    assert touched == []


# --- the factory --------------------------------------------------------------------------------


def test_node_is_checked_before_chrome_is_started(store, bus, monkeypatch) -> None:
    """A machine that cannot drive a browser must never have one opened for it."""
    ensures = []

    def _no_node(command):
        raise BrowserUnavailable("'npx' is not installed")

    monkeypatch.setattr(daemon.browser_client, "ensure_available", _no_node)
    monkeypatch.setattr(
        daemon.chrome,
        "ensure_running",
        lambda port, **kw: ensures.append(port) or (chrome.READY, 9222),
    )

    factory = daemon.make_browser_factory(Config(), store, bus, {})
    with pytest.raises(BrowserUnavailable):
        factory()
    assert ensures == []


def test_every_acquisition_re_ensures_chrome_and_reuses_the_client(store, bus, monkeypatch) -> None:
    """The client is cached for the daemon's life, but Chrome can be closed at any point after —
    so the ensure runs per call, outside the construction guard."""
    ensures = []
    monkeypatch.setattr(daemon.browser_client, "ensure_available", lambda command: None)
    monkeypatch.setattr(
        daemon.chrome,
        "ensure_running",
        lambda port, **kw: ensures.append(port) or (chrome.READY, 9222),
    )

    holder: dict = {}
    factory = daemon.make_browser_factory(Config(), store, bus, holder)
    first = factory()
    second = factory()
    assert first is second is holder["client"]
    assert len(ensures) == 2


def test_a_chrome_that_came_back_on_a_new_port_gets_a_new_client(store, bus, monkeypatch) -> None:
    """An unpinned Chrome that was closed and restarted is on a different port, and the cached
    client still dials the old one — every call on it would fail."""
    ports = iter([45123, 46001])
    closed = []
    monkeypatch.setattr(daemon.browser_client, "ensure_available", lambda command: None)
    monkeypatch.setattr(
        daemon.chrome, "ensure_running", lambda port, **kw: (chrome.READY, next(ports))
    )
    monkeypatch.setattr(
        daemon.browser_client.BrowserClient, "close", lambda self: closed.append(self)
    )

    holder: dict = {}
    factory = daemon.make_browser_factory(Config(), store, bus, holder)
    first = factory()
    second = factory()
    assert first is not second
    assert closed == [first]
    assert "http://127.0.0.1:46001" in holder["command"]


def test_every_acquisition_re_reads_watch_mode(store, bus, monkeypatch) -> None:
    """The seller flips watch mode in chat; the lanes hold a cached client. Reading it here is what
    makes a tap reach the read lane, the send and the attended tools from their next call on."""
    from sellee import settings

    monkeypatch.setattr(daemon.browser_client, "ensure_available", lambda command: None)
    monkeypatch.setattr(daemon.chrome, "ensure_running", lambda port, **kw: (chrome.READY, 9222))

    holder: dict = {}
    factory = daemon.make_browser_factory(Config(), store, bus, holder)
    assert factory()._follow is False  # noqa: SLF001 — the flag is the wiring under test

    settings.set_now(store, bus, key="watch_browser", raw_value=True)
    assert factory()._follow is True  # noqa: SLF001 — same cached client, re-read setting


# --- finding the binary --------------------------------------------------------------------------


def test_a_configured_path_wins_on_every_platform(monkeypatch) -> None:
    """Checked first: a seller with Chrome somewhere unusual said so in config, and no amount of
    searching should second-guess that."""
    for platform in ("darwin", "linux"):
        monkeypatch.setattr(chrome.sys, "platform", platform)
        assert chrome.resolve_binary("/opt/my-chrome") == "/opt/my-chrome"


def test_macos_answers_the_one_place_chrome_installs(monkeypatch) -> None:
    monkeypatch.setattr(chrome.sys, "platform", "darwin")
    assert chrome.resolve_binary() == chrome._CHROME_MACOS


def test_linux_takes_the_first_candidate_it_can_find(monkeypatch) -> None:
    """There is no single install location on Linux, so the ladder decides — Google's own package
    ahead of a distribution's Chromium, since that is what the live install is tested against."""
    monkeypatch.setattr(chrome.sys, "platform", "linux")
    present = {"chromium": "/usr/bin/chromium", "google-chrome": "/usr/bin/google-chrome"}
    monkeypatch.setattr(chrome.shutil, "which", present.get)
    assert chrome.resolve_binary() == "/usr/bin/google-chrome"

    del present["google-chrome"]
    assert chrome.resolve_binary() == "/usr/bin/chromium"


def test_linux_falls_back_to_the_path_the_deb_installs(monkeypatch, tmp_path) -> None:
    """A supervised worker's PATH is minimal, so `which` can come up empty on a machine that does
    have Chrome. The absolute path Google's .deb writes is the last thing tried."""
    monkeypatch.setattr(chrome.sys, "platform", "linux")
    monkeypatch.setattr(chrome.shutil, "which", lambda name: None)
    real = tmp_path / "opt" / "google" / "chrome" / "chrome"
    real.parent.mkdir(parents=True)
    real.write_text("")
    monkeypatch.setattr(chrome, "_CHROME_LINUX", ("google-chrome", str(real)))
    assert chrome.resolve_binary() == str(real)


def test_linux_with_no_chrome_answers_something_a_person_recognises(monkeypatch) -> None:
    """The installer's gate reports "not found at X". An empty X would tell a seller nothing;
    the package name tells them what to install."""
    monkeypatch.setattr(chrome.sys, "platform", "linux")
    monkeypatch.setattr(chrome.shutil, "which", lambda name: None)
    monkeypatch.setattr(chrome, "_CHROME_LINUX", ("google-chrome", "/opt/nowhere/chrome"))
    assert chrome.resolve_binary() == "google-chrome"


def test_the_profile_locks_are_named_the_same_on_both_posix_platforms() -> None:
    """Chrome writes these itself; they are not ours to translate per OS."""
    assert chrome.SINGLETON_LOCKS == ("SingletonLock", "SingletonCookie", "SingletonSocket")


# --- giving up on a launch when the daemon is stopping --------------------------------------------


@pytest.fixture
def launch_that_never_answers(monkeypatch):
    """Chrome starts and never binds its port — what a headless machine does, and what a wrong
    binary on the discovery ladder does."""
    monkeypatch.setattr(chrome, "_last_failed_launch_ts", None)
    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: False)
    monkeypatch.setattr(chrome, "clear_stale_locks", lambda: [])
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda *a, **kw: None)


def test_a_stop_during_the_launch_wait_gives_up_promptly(launch_that_never_answers) -> None:
    """The daemon drains by waiting for its lanes, so a lane sitting out the whole launch wait is a
    `daemon stop` that looks wedged for 20s. Chrome is detached and keeps coming up regardless."""
    began = time.monotonic()
    state, port = chrome.ensure_running(9222, wait_sec=30.0, should_stop=lambda: True)

    assert (state, port) == (chrome.UNAVAILABLE, None)
    assert time.monotonic() - began < 5.0


def test_a_daemon_that_is_not_stopping_still_waits_the_launch_out(
    launch_that_never_answers,
) -> None:
    """The predicate must not short-circuit an ordinary launch: a cold Chrome on a large profile
    takes seconds, and giving up early would report a browser that was about to be there."""
    began = time.monotonic()
    state, port = chrome.ensure_running(9222, wait_sec=1.0, should_stop=lambda: False)

    assert (state, port) == (chrome.UNAVAILABLE, None)
    assert time.monotonic() - began >= 1.0


# --- replacing a server that lost Chrome ---------------------------------------------------------


class FakeClient:
    """Just the surface the factory reads. The transport is covered in test_browser_client.py."""

    def __init__(self, *, streak=0, age=0.0):
        self.streak = streak
        self.age = age
        self.closed_gracefully: bool | None = None
        self.detached_as: str | None = None
        self.follow = False

    def failing_streak(self):
        return self.streak

    def age_sec(self, *, now=time.monotonic):
        return self.age

    def mark_detached(self, reason):
        self.detached_as = reason

    def set_follow(self, follow):
        self.follow = follow

    def close(self, *, graceful=True):
        self.closed_gracefully = graceful


@pytest.fixture
def factory_bits(store, bus, monkeypatch):
    """A factory whose Chrome is always up and whose clock the test drives."""
    monkeypatch.setattr(daemon.browser_client, "ensure_available", lambda command: None)
    monkeypatch.setattr(daemon.chrome, "ensure_running", lambda port, **kw: (chrome.READY, 9222))
    monkeypatch.setattr(daemon.chrome, "is_ready", lambda port, **kw: True)
    monkeypatch.setattr(daemon.chrome, "page_targets", lambda port, **kw: 1)
    monkeypatch.setattr(daemon.browser_client, "BrowserClient", lambda **kw: FakeClient())
    clock = {"t": 10_000.0}
    holder: dict = {}
    factory = daemon.make_browser_factory(Config(), store, bus, holder, now=lambda: clock["t"])
    return factory, holder, clock


def _recycled(bus):
    return [ev for ev in bus.store.read() if ev.kind == "browser.recycled"]


def test_a_healthy_client_is_never_recycled(store, bus, factory_bits) -> None:
    """The steady state, and the guard against a diagnosis creeping onto the hot path."""
    factory, holder, _clock = factory_bits
    first = factory()
    assert factory() is first
    assert _recycled(bus) == []


def test_a_server_that_lost_chrome_is_replaced(store, bus, factory_bits) -> None:
    """The 2026-08-27 shape: the process answers us, every tool fails, and Chrome is fine. Until
    this, `_start` only respawned a *dead* process, so a wedged live one was immortal — 126 blind
    reads over 28 hours."""
    factory, holder, _clock = factory_bits
    stale = FakeClient(streak=daemon.browser_client.RECYCLE_AFTER_FAILURES)
    holder["client"] = stale
    holder["command"] = daemon.browser_client.default_command("http://127.0.0.1:9222")

    fresh = factory()
    assert fresh is not stale
    assert stale.detached_as == daemon.DETACHED_REASON
    # Never asked to close its tab: on a server that has lost Chrome that call cannot succeed and
    # cannot fail quickly either, and it would wait out the full tool timeout holding the lock.
    assert stale.closed_gracefully is False
    assert [ev.payload["reason"] for ev in _recycled(bus)] == [daemon.DETACHED_REASON]


def test_a_failing_server_is_not_blamed_while_chrome_is_down(store, bus, factory_bits, monkeypatch):
    """Both halves are needed. A marketplace that redesigned its DOM also fails every call, and
    respawning node for that would hide a broken adapter forever."""
    factory, holder, _clock = factory_bits
    monkeypatch.setattr(daemon.chrome, "is_ready", lambda port, **kw: False)
    stale = FakeClient(streak=daemon.browser_client.RECYCLE_AFTER_FAILURES)
    holder["client"] = stale
    holder["command"] = daemon.browser_client.default_command("http://127.0.0.1:9222")
    assert factory() is stale
    assert _recycled(bus) == []


def test_an_old_server_is_swapped_gracefully(store, bus, factory_bits) -> None:
    """The age ceiling. Unlike a detach this one closes its own tab first, so the incoming client
    opens one and the seller sees no change at all."""
    factory, holder, _clock = factory_bits
    old = FakeClient(age=daemon.BROWSER_RECYCLE_AGE_SEC + 1.0)
    holder["client"] = old
    holder["command"] = daemon.browser_client.default_command("http://127.0.0.1:9222")
    assert factory() is not old
    assert old.closed_gracefully is True
    assert old.detached_as is None  # nothing is wrong with it; it is only old
    assert [ev.payload["reason"] for ev in _recycled(bus)] == [daemon.AGE_REASON]


def test_a_pass_driving_the_browser_is_never_interrupted(store, bus, factory_bits, monkeypatch):
    """A publish holds the tab across many calls without the client's own mutex, so replacing its
    server mid-flow abandons a half-filled listing form and starts again from nothing."""
    factory, holder, _clock = factory_bits
    monkeypatch.setattr(daemon.inbox, "browser_pass_running", lambda store: True)
    stale = FakeClient(streak=daemon.browser_client.RECYCLE_AFTER_FAILURES)
    holder["client"] = stale
    holder["command"] = daemon.browser_client.default_command("http://127.0.0.1:9222")
    assert factory() is stale
    assert _recycled(bus) == []


def test_the_cooldown_stops_a_fast_lane_draining_the_allowance(store, bus, factory_bits) -> None:
    """The connect lane acquires every two seconds while a sign-in row is pending — and a pending
    row is exactly what the can't-read notice produces. Without a cooldown three of its ticks would
    spend the hour's allowance in six seconds and leave the read lane reporting the browser
    unavailable for the other fifty-nine minutes."""
    factory, holder, clock = factory_bits
    command = daemon.browser_client.default_command("http://127.0.0.1:9222")
    for _ in range(4):
        holder["client"] = FakeClient(streak=daemon.browser_client.RECYCLE_AFTER_FAILURES)
        holder["command"] = command
        factory()
        clock["t"] += 2.0
    assert len(_recycled(bus)) == 1  # only the first; the rest were inside the cooldown


def test_replacing_it_stops_once_it_has_stopped_helping(store, bus, factory_bits) -> None:
    """Respawning node forever is how a bug becomes invisible. The fourth try in an hour reports
    the browser undrivable instead, through the notice that already exists for that."""
    factory, holder, clock = factory_bits
    command = daemon.browser_client.default_command("http://127.0.0.1:9222")
    for _ in range(daemon.BROWSER_RECYCLE_MAX):
        holder["client"] = FakeClient(streak=daemon.browser_client.RECYCLE_AFTER_FAILURES)
        holder["command"] = command
        factory()
        clock["t"] += daemon.BROWSER_RECYCLE_COOLDOWN_SEC + 1.0
    assert len(_recycled(bus)) == daemon.BROWSER_RECYCLE_MAX

    holder["client"] = FakeClient(streak=daemon.browser_client.RECYCLE_AFTER_FAILURES)
    holder["command"] = command
    with pytest.raises(BrowserUnavailable) as exc:
        factory()
    assert daemon.RECYCLE_EXHAUSTED_REASON in str(exc.value)


def test_the_window_lets_the_daemon_try_again_by_itself(store, bus, factory_bits) -> None:
    """Giving up has to be temporary, or a machine that wedged once at 3am is dead until someone
    restarts the daemon."""
    factory, holder, clock = factory_bits
    command = daemon.browser_client.default_command("http://127.0.0.1:9222")
    for _ in range(daemon.BROWSER_RECYCLE_MAX):
        holder["client"] = FakeClient(streak=daemon.browser_client.RECYCLE_AFTER_FAILURES)
        holder["command"] = command
        factory()
        clock["t"] += daemon.BROWSER_RECYCLE_COOLDOWN_SEC + 1.0

    clock["t"] += daemon.BROWSER_RECYCLE_WINDOW_SEC
    holder["client"] = FakeClient(streak=daemon.browser_client.RECYCLE_AFTER_FAILURES)
    holder["command"] = command
    factory()  # no raise
    assert len(_recycled(bus)) == daemon.BROWSER_RECYCLE_MAX + 1


def test_a_chrome_that_moved_port_is_not_charged_against_the_allowance(store, bus, factory_bits):
    """A seller restarting Chrome a few times must not spend the budget for a fault that is not
    the server's."""
    factory, holder, _clock = factory_bits
    holder["client"] = FakeClient()
    holder["command"] = ["some", "older", "command"]
    factory()
    assert holder.get("recycles", ()) == ()
    assert _recycled(bus) == []


# --- the window the seller closed ----------------------------------------------------------------


def test_a_windowless_chrome_is_explained_once(store, bus, factory_bits, monkeypatch) -> None:
    """Closing the window does not quit Chrome on macOS, and the browser server opens a tab for any
    page tool when it has none — so a window comes back on the next read whatever we do. The only
    honest thing is to say so, and to ask for a minimise instead."""
    factory, _holder, _clock = factory_bits
    monkeypatch.setattr(daemon.chrome, "page_targets", lambda port, **kw: 0)
    factory()
    factory()
    assert _notices(store) == [daemon.CHROME_WINDOW_REOPENED_NOTICE]


def test_the_window_notice_re_arms_once_a_window_exists_again(
    store, bus, factory_bits, monkeypatch
):
    """Said each time they close it, never twice for the same one."""
    factory, _holder, _clock = factory_bits
    pages = {"n": 0}
    monkeypatch.setattr(daemon.chrome, "page_targets", lambda port, **kw: pages["n"])
    factory()
    pages["n"] = 1
    factory()
    pages["n"] = 0
    factory()
    assert _notices(store) == [daemon.CHROME_WINDOW_REOPENED_NOTICE] * 2


def test_a_chrome_that_cannot_be_asked_says_nothing(store, bus, factory_bits, monkeypatch) -> None:
    """ "We could not ask" must never read as "there are no windows"."""
    factory, _holder, _clock = factory_bits
    monkeypatch.setattr(daemon.chrome, "page_targets", lambda port, **kw: None)
    factory()
    assert _notices(store) == []


# --- the window has to be wide enough for the marketplace to serve its desktop layout ------------


def _factory(store, bus, monkeypatch, width, calls):
    """A browser factory whose Chrome is ready and whose window reports `width`."""
    monkeypatch.setattr(daemon.chrome, "ensure_running", lambda port, **kw: (chrome.READY, 9222))
    monkeypatch.setattr(daemon.browser_client, "ensure_available", lambda command: None)
    monkeypatch.setattr(
        daemon.chrome,
        "ensure_window_width",
        lambda port, minimum, **kw: calls.append((port, minimum)) or width,
    )

    class _Stub:
        def set_follow(self, on):
            pass

        def failing_streak(self):
            return 0

        def age_sec(self, now=None):
            return 0.0

    monkeypatch.setattr(daemon.browser_client, "BrowserClient", lambda **kw: _Stub())
    return daemon.make_browser_factory(Config(), store, bus, {})


def test_every_acquisition_checks_the_window_width(store, bus, monkeypatch) -> None:
    """Not once at launch: --restore-last-session brings back whatever width the window last had,
    so a window narrowed once would otherwise stay narrow across every restart."""
    calls: list = []
    factory = _factory(store, bus, monkeypatch, 1600, calls)
    factory()
    factory()

    assert calls == [(9222, blindness.MIN_USABLE_WIDTH_PX)] * 2


def test_a_window_that_stays_narrow_is_evented_not_raised(store, bus, monkeypatch) -> None:
    """A window we cannot widen must not fail the acquisition — the read may still work, and if it
    does not, the reader's own measurements promote it to CAUSE_VIEWPORT for the seller."""
    factory = _factory(store, bus, monkeypatch, 756, [])
    factory()

    narrow = bus.store.read(kinds=["browser.window_narrow"])
    assert [e.payload["width"] for e in narrow] == [756]
    assert narrow[0].payload["needed"] == blindness.MIN_USABLE_WIDTH_PX


def test_a_wide_enough_window_says_nothing(store, bus, monkeypatch) -> None:
    """Steady state is silent: the event exists to explain a failure, not to narrate success."""
    factory = _factory(store, bus, monkeypatch, 1600, [])
    factory()

    assert bus.store.read(kinds=["browser.window_narrow"]) == []


def test_a_window_that_cannot_be_measured_is_not_reported_as_narrow(
    store, bus, monkeypatch
) -> None:
    """0 means "we could not ask", which must never read as "the window is too small"."""
    factory = _factory(store, bus, monkeypatch, 0, [])
    factory()

    assert bus.store.read(kinds=["browser.window_narrow"]) == []
