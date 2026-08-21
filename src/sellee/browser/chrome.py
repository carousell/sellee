"""The warm Chrome the browser layer attaches to: readiness probe, launch invocation, bring-up.

One dedicated profile means exactly one Chrome may own it, and a second launch on a live profile
either hangs or opens read-only — so nothing here launches without the probe first saying the port
is silent. Given that, the daemon may start Chrome itself: a background publish that arrives while
the window happens to be closed is worth a window opening for, and the seller is told before it
does. Keeping it alive across crashes and logins is still the supervisor's job, not this module's.

Where the browser is on a different machine from the daemon — a container talking to the seller's
own desktop Chrome — launching is not ours to do at all, and `ensure_running(may_launch=False)`
answers with the probe alone.

The probe is the only network I/O in the browser layer: an HTTP GET of Chrome's own CDP version
endpoint on the loopback interface. It has to establish more than "something answered": the port
carries unauthenticated control of a browser holding the seller's marketplace sessions, so a
responder that is not the Chrome we launched must not be mistaken for one.

Nothing here reads config. Every entry point takes the port — or `None`, meaning "let Chrome pick
one" — from its caller.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from sellee import paths

log = logging.getLogger(__name__)

_PROBE_TIMEOUT_SEC = 2.0

# How long a launched Chrome gets to answer on its debugging port, and how often we ask. A cold
# start on a large profile is seconds; anything past this is a Chrome that is not coming up, and the
# caller retries on its own schedule rather than blocking a lane on it.
LAUNCH_WAIT_SEC = 20.0
_LAUNCH_POLL_SEC = 0.25

# One launch at a time: two callers arriving in the same window would both see a silent port, both
# clear the profile locks, and both launch — two Chromes contending one profile. The loser waits,
# re-probes under the lock, finds the port answering, and never launches at all.
_LAUNCH_LOCK = threading.Lock()

# A launch that never answered cost its caller the full wait, so one failure quiets further
# attempts for this long — the callers that keep asking (the lanes tick every 30–300s) answer
# UNAVAILABLE immediately instead of each burning another launch and wait on a Chrome that is
# not coming.
FAILED_LAUNCH_BACKOFF_SEC = 300.0
_last_failed_launch_ts: float | None = None

_CHROME_MACOS = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# What a Linux desktop calls Chrome, most-preferred first: Google's own .deb/.rpm provides
# `google-chrome`, the rest are the distributions' Chromium packages. The absolute path last is
# where that .deb puts the binary, for a session whose PATH does not reach /usr/bin.
_CHROME_LINUX = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "/opt/google/chrome/chrome",
)

# What ensure_running found or did.
READY = "ready"
LAUNCHED = "launched"
UNAVAILABLE = "unavailable"

# Locks a SIGKILLed Chrome leaves behind. They make the next launch on the same profile hang, so the
# bring-up clears them — safe only because it has already established that no Chrome is answering.
SINGLETON_LOCKS = ("SingletonLock", "SingletonCookie", "SingletonSocket")

# Where Chrome announces the port it bound: line 1 the port, line 2 the browser WebSocket path.
# A killed Chrome leaves it behind, so it is cleared with the locks — otherwise the probe aims at a
# dead port something else may have taken since.
ACTIVE_PORT_FILE = "DevToolsActivePort"

# Used when nothing is pinned and no live Chrome has announced a port. It is the number the by-hand
# launch instruction prints and the one the container's forwarder listens on — both agreements with
# a process that cannot read this profile.
DEFAULT_CDP_PORT = 9222


def resolve_binary(chrome_bin: str | None = None) -> str:
    """The Chrome executable to drive: the configured path, or the OS default install location.

    One answer for the launch, the by-hand hint, and the installer's "is Chrome even here" gate —
    a gate that checked a different path from the one the launch uses would pass and then fail.
    """
    if chrome_bin:
        return chrome_bin
    if sys.platform == "linux":
        return _linux_chrome()
    return _CHROME_MACOS


def _linux_chrome() -> str:
    """The first candidate this machine actually has, or the name to install when it has none.

    A name rather than nothing, because the installer reports "not found at <this>" — which a
    person can act on where an empty path is not.
    """
    for candidate in _CHROME_LINUX:
        if candidate.startswith("/"):
            if Path(candidate).exists():
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return _CHROME_LINUX[0]


def version_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/json/version"


def _read_active_port_file() -> tuple | None:
    """Chrome's announced `(port, ws_path)`, or None when it has announced nothing readable.

    A first line that is not a port means a file we did not write, and the only safe reading of
    that is "no Chrome".
    """
    try:
        text = (paths.browser_profile_dir() / ACTIVE_PORT_FILE).read_text("utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    if not lines:
        return None
    try:
        port = int(lines[0].strip())
    except ValueError:
        return None
    if not (1 <= port <= 65535):
        return None
    ws_path = lines[1].strip() if len(lines) > 1 else ""
    return port, ws_path


def active_port() -> int | None:
    """The port the Chrome on this profile last announced, or None if it announced none."""
    found = _read_active_port_file()
    return None if found is None else found[0]


def resolve_port(configured: int | None) -> int:
    """The CDP port to dial for a Chrome nobody is launching right now.

    A pinned port wins, then whatever a live Chrome announced. The fallback is reached only when
    there is neither, and it is the same number the by-hand instruction prints, so callers that
    only build an endpoint still find a Chrome the seller started themselves.
    """
    if configured is not None:
        return configured
    found = active_port()
    return found if found is not None else DEFAULT_CDP_PORT


def _is_our_chrome(payload: object, port: int) -> bool:
    """Whether a `/json/version` answer came from the Chrome on this profile.

    Any local process can bind a loopback port and serve plausible JSON, and the daemon would point
    Playwright at it — where it could feed the agent fabricated pages and read back what the agent
    types. So the answer must identify itself as Chrome and, where Chrome announced a WebSocket
    path, name the same one: that path is per browser instance and lives in a `0700` profile.
    """
    if not isinstance(payload, dict):
        return False
    browser = payload.get("Browser")
    if not isinstance(browser, str) or not browser.startswith(
        ("Chrome/", "Chromium/", "HeadlessChrome/")
    ):
        return False
    announced = _read_active_port_file()
    if announced is None or announced[0] != port or not announced[1]:
        return True
    url = payload.get("webSocketDebuggerUrl")
    if not isinstance(url, str):
        return False
    return urllib.parse.urlparse(url).path == announced[1]


def is_ready(port: int, *, timeout_sec: float = _PROBE_TIMEOUT_SEC) -> bool:
    """Whether the Chrome we drive is answering on this port — not merely whether something is."""
    try:
        with urllib.request.urlopen(version_url(port), timeout=timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return False
    if not _is_our_chrome(payload, port):
        log.warning("something other than the agent's Chrome is answering on port %s", port)
        return False
    return True


def clear_stale_locks() -> list:
    """Remove the singleton lock files and the announced port from the profile, returning what went.

    Only ever called once the probe says no Chrome is answering: at that moment anything in here was
    left by a Chrome that was killed before it could clean up. The locks would hang the next launch;
    the announced port would send the next probe at a port this Chrome no longer holds.
    """
    removed = []
    profile = paths.browser_profile_dir()
    for name in (*SINGLETON_LOCKS, ACTIVE_PORT_FILE):
        lock = profile / name
        try:
            lock.unlink()
        except OSError:
            continue
        removed.append(name)
    return removed


def launch_command(port: int | None, *, chrome_bin: str | None = None) -> list:
    """The argv for the warm Chrome: the agent's own profile, CDP open on the loopback interface.

    A dedicated profile is the point — the seller's everyday Chrome is never driven, and the
    marketplace sessions the agent uses persist here across restarts.

    `port=None` asks Chrome for a free one (`--remote-debugging-port=0`) and reads it back out of
    the profile. Nothing can squat a port that is not chosen until Chrome chooses it, and a port
    only readable from a `0700` directory is not one another local user can find.

    `--disable-backgrounding-occluded-windows` keeps a window the seller has covered with another
    app from counting as hidden, which spares every send the work of raising a tab that was already
    active.
    """
    return [
        resolve_binary(chrome_bin),
        f"--remote-debugging-port={0 if port is None else port}",
        f"--user-data-dir={paths.browser_profile_dir()}",
        "--disable-backgrounding-occluded-windows",
        "--no-first-run",
        "--no-default-browser-check",
        "--restore-last-session",
        "--hide-crash-restore-bubble",
        "--window-position=80,80",
        "--window-size=1200,900",
    ]


def bring_up_hint(port: int | None, *, chrome_bin: str | None = None) -> str:
    """What to tell a person whose Chrome could not be started for them. Installing the launchd job
    that keeps it alive is the installer's work; this is the by-hand instruction.

    A person needs a number, so an unpinned port resolves to one here rather than telling them to
    type a zero. It resolves to the same number the endpoint-building callers fall back to, so the
    Chrome they start by hand is the Chrome the daemon then finds.
    """
    concrete = resolve_port(port)
    argv = launch_command(concrete, chrome_bin=chrome_bin)
    quoted = " ".join(f'"{part}"' if " " in part else part for part in argv)
    return f"the agent's Chrome is not running on port {concrete} — start it with:\n  {quoted}"


# The one-line version of the instruction below, for a report line that has room for a fix and
# not for an argv.
CONTAINER_CHROME_FIX = (
    "Start Chrome on your own computer: ./start-chrome.sh (start-chrome.ps1 on Windows)."
)


def container_bring_up_hint(port: int) -> str:
    """The same instruction where the browser is not ours to start.

    Naming the argv here would be worse than useless: it describes a Chrome on the machine running
    this code, and the one that matters is on the seller's desktop, with its own profile directory
    and its own path to the binary. The script that knows those ships beside the compose file.
    """
    return (
        f"the agent's Chrome is not answering on port {port} — it runs on your own computer, "
        "not in the container. Start it from the sellee checkout with:\n"
        "  ./start-chrome.sh      (macOS, Linux)\n"
        "  .\\start-chrome.ps1     (Windows PowerShell)"
    )


def _live_port(port: int | None) -> int | None:
    """The port our Chrome is answering on right now, or None.

    With no pin the port is whatever Chrome announced, so a missing announcement is itself the
    answer "not running" — the probe is never sent at a port we only guessed, which is what makes
    a squatter unreachable rather than merely unlikely.
    """
    found = port if port is not None else active_port()
    if found is None:
        return None
    return found if is_ready(found) else None


def ensure_running(
    port: int | None,
    *,
    chrome_bin: str | None = None,
    wait_sec: float = LAUNCH_WAIT_SEC,
    may_launch: bool = True,
    should_stop=None,
):
    """Make sure the agent's Chrome is answering, starting it if it is not, and say on which port.

    Answers `(READY, port)` (it already was), `(LAUNCHED, port)` (it is now, and the seller should
    be told a window appeared), or `(UNAVAILABLE, None)` (it is not, and the caller should do
    nothing that needs a browser).

    `port=None` means the port is Chrome's to choose: it is launched with
    `--remote-debugging-port=0` and the resolved port is read back from the profile. The port
    therefore comes *out* of this call, and callers must build their CDP endpoints from what it
    returns rather than from config.

    The probe comes first and decides everything: only once the port is silent is a lock left by a
    killed Chrome safe to clear, and only then is a second launch on this profile safe at all. The
    whole body runs under one launch lock so that holds with concurrent callers, and a launch that
    failed quiets further attempts for FAILED_LAUNCH_BACKOFF_SEC.

    `may_launch=False` reduces all of that to the probe: no binary resolved, no lock file touched,
    nothing started. That is the only correct behavior where the browser belongs to a machine this
    process is not running on — the profile whose locks it would clear is not the profile that
    Chrome uses, and the binary it would resolve is not the one the seller runs.
    """
    global _last_failed_launch_ts
    with _LAUNCH_LOCK:
        live = _live_port(port)
        if live is not None:
            _last_failed_launch_ts = None
            return READY, live

        if not may_launch:
            return UNAVAILABLE, None

        if (
            _last_failed_launch_ts is not None
            and time.monotonic() - _last_failed_launch_ts < FAILED_LAUNCH_BACKOFF_SEC
        ):
            return UNAVAILABLE, None

        removed = clear_stale_locks()
        if removed:
            log.info("cleared stale Chrome profile file(s): %s", ", ".join(removed))

        argv = launch_command(port, chrome_bin=chrome_bin)
        try:
            # Its own session, so the daemon's exit — or a pass group being killed — never takes the
            # seller's browser with it.
            subprocess.Popen(  # noqa: S603 — argv is composed by launch_command, not a shell
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            log.warning("could not start Chrome (%s): %s", argv[0], exc)
            _last_failed_launch_ts = time.monotonic()
            return UNAVAILABLE, None

        deadline = time.monotonic() + wait_sec
        while time.monotonic() < deadline:
            time.sleep(_LAUNCH_POLL_SEC)
            live = _live_port(port)
            if live is not None:
                _last_failed_launch_ts = None
                return LAUNCHED, live
            if should_stop is not None and should_stop():
                # The daemon drains by waiting for its lanes, so a lane still sitting out this wait
                # is a stop that looks wedged. Chrome is detached and comes up on its own; the next
                # acquisition finds it ready.
                log.info("stopping while waiting for Chrome — leaving it to start")
                return UNAVAILABLE, None
        log.warning("started Chrome but it did not answer within %ss", wait_sec)
        _last_failed_launch_ts = time.monotonic()
        return UNAVAILABLE, None
