"""The warm Chrome the browser layer attaches to: readiness probe, launch invocation, bring-up.

One dedicated profile means exactly one Chrome may own it, and a second launch on a live profile
either hangs or opens read-only — so nothing here launches without the probe first saying the port
is silent. Given that, the daemon may start Chrome itself: a background publish that arrives while
the window happens to be closed is worth a window opening for, and the seller is told before it
does. Keeping it alive across crashes and logins is still the supervisor's job, not this module's.

The probe is the only network I/O in the browser layer: an HTTP GET of Chrome's own CDP version
endpoint on the loopback interface.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from selly_agent import paths, spawn

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

# Windows installs Chrome per-machine or per-user, so there is no single path to name — and the
# registry entry is what an installer of either kind writes.
_CHROME_WINDOWS_APP_PATHS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
_CHROME_WINDOWS_RELATIVE = r"Google\Chrome\Application\chrome.exe"

# What ensure_running found or did.
READY = "ready"
LAUNCHED = "launched"
UNAVAILABLE = "unavailable"


def resolve_binary(chrome_bin: str | None = None) -> str:
    """The Chrome executable to drive: the configured path, or where this OS keeps it.

    One answer for the launch, the by-hand hint, and the installer's "is Chrome even here" gate —
    a gate that checked a different path from the one the launch uses would pass and then fail.

    The last candidate is returned even when nothing exists, so the caller reports a path that was
    actually looked for rather than an empty string.
    """
    if chrome_bin:
        return chrome_bin
    candidates = chrome_candidates()
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return candidates[-1]


def chrome_candidates() -> list:
    """Where Chrome might be, most authoritative first.

    macOS has one install location worth naming. Windows has several, because an install can be
    per-machine or per-user, so the registry entry that either kind writes is asked first and the
    conventional directories are the fallback. Elsewhere it is a name on PATH, under any of the
    three the distributions use.
    """
    if sys.platform == "darwin":
        return [_CHROME_MACOS]
    if os.name == "nt":
        found = _registered_chrome()
        roots = [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            str(paths.local_app_data()),
        ]
        conventional = [str(Path(root) / _CHROME_WINDOWS_RELATIVE) for root in roots if root]
        return ([found] if found else []) + conventional
    on_path = [shutil.which(name) for name in ("google-chrome", "chromium", "chromium-browser")]
    return [found for found in on_path if found] or ["google-chrome"]


def _registered_chrome() -> str | None:
    """Chrome's path as its own installer recorded it, or None when nothing did."""
    import winreg

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, _CHROME_WINDOWS_APP_PATHS) as key:
                value, _kind = winreg.QueryValueEx(key, "")
                if value:
                    return str(Path(value.strip('"')))
        except OSError:
            continue
    return None


def singleton_lock_names() -> tuple:
    """The lock names a Chrome killed before it could clean up leaves in its profile.

    Per-OS because they differ, and clearing the wrong names is worse than clearing none: the next
    launch on the profile hangs, which is the failure this clearing exists to prevent.
    """
    if os.name == "nt":
        return ("lockfile",)
    return ("SingletonLock", "SingletonCookie", "SingletonSocket")


def version_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/json/version"


def is_ready(port: int, *, timeout_sec: float = _PROBE_TIMEOUT_SEC) -> bool:
    """Whether Chrome's CDP endpoint is answering on this port."""
    try:
        with urllib.request.urlopen(version_url(port), timeout=timeout_sec) as resp:
            json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return True


def clear_stale_locks() -> list:
    """Remove the singleton lock files from the profile, returning what was removed.

    Only ever called once the probe says no Chrome is answering: at that moment any lock in the
    profile was left by a Chrome that was killed before it could clean up.
    """
    removed = []
    profile = paths.browser_profile_dir()
    for name in singleton_lock_names():
        lock = profile / name
        try:
            lock.unlink()
        except OSError:
            continue
        removed.append(name)
    return removed


def launch_command(port: int, *, chrome_bin: str | None = None) -> list:
    """The argv for the warm Chrome: the agent's own profile, CDP open on the loopback interface.

    A dedicated profile is the point — the seller's everyday Chrome is never driven, and the
    marketplace sessions the agent uses persist here across restarts.

    `--disable-backgrounding-occluded-windows` keeps a window the seller has covered with another
    app from counting as hidden, which spares every send the work of raising a tab that was already
    active.
    """
    return [
        resolve_binary(chrome_bin),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={paths.browser_profile_dir()}",
        "--disable-backgrounding-occluded-windows",
        "--no-first-run",
        "--no-default-browser-check",
        "--restore-last-session",
        "--hide-crash-restore-bubble",
        "--window-position=80,80",
        "--window-size=1200,900",
    ]


def bring_up_hint(port: int, *, chrome_bin: str | None = None) -> str:
    """What to tell a person whose Chrome could not be started for them. Installing the launchd job
    that keeps it alive is the installer's work; this is the by-hand instruction."""
    argv = launch_command(port, chrome_bin=chrome_bin)
    quoted = " ".join(f'"{part}"' if " " in part else part for part in argv)
    return f"the agent's Chrome is not running on port {port} — start it with:\n  {quoted}"


def ensure_running(port: int, *, chrome_bin: str | None = None, wait_sec: float = LAUNCH_WAIT_SEC):
    """Make sure the agent's Chrome is answering on its debugging port, starting it if it is not.

    Answers READY (it already was), LAUNCHED (it is now, and the seller should be told a window
    appeared), or UNAVAILABLE (it is not, and the caller should do nothing that needs a browser).

    The probe comes first and decides everything: only once the port is silent is a lock left by a
    killed Chrome safe to clear, and only then is a second launch on this profile safe at all. The
    whole body runs under one launch lock so that holds with concurrent callers, and a launch that
    failed quiets further attempts for FAILED_LAUNCH_BACKOFF_SEC.
    """
    global _last_failed_launch_ts
    with _LAUNCH_LOCK:
        if is_ready(port):
            _last_failed_launch_ts = None
            return READY

        if (
            _last_failed_launch_ts is not None
            and time.monotonic() - _last_failed_launch_ts < FAILED_LAUNCH_BACKOFF_SEC
        ):
            return UNAVAILABLE

        removed = clear_stale_locks()
        if removed:
            log.info("cleared stale Chrome profile lock(s): %s", ", ".join(removed))

        argv = launch_command(port, chrome_bin=chrome_bin)
        try:
            # Detached, so the daemon's exit — or a pass group being killed — does not take the
            # browser with it. Not absolute: the forced kill of a wedged daemon still walks its
            # children, and this Chrome is one; see spawn.survives_us_flags.
            subprocess.Popen(  # noqa: S603 — argv is composed by launch_command, not a shell
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **spawn.survives_us_flags(),
            )
        except OSError as exc:
            log.warning("could not start Chrome (%s): %s", argv[0], exc)
            _last_failed_launch_ts = time.monotonic()
            return UNAVAILABLE

        deadline = time.monotonic() + wait_sec
        while time.monotonic() < deadline:
            time.sleep(_LAUNCH_POLL_SEC)
            if is_ready(port):
                _last_failed_launch_ts = None
                return LAUNCHED
        log.warning("started Chrome but it did not answer on port %s within %ss", port, wait_sec)
        _last_failed_launch_ts = time.monotonic()
        return UNAVAILABLE
