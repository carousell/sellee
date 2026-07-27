"""The warm Chrome the browser layer attaches to: readiness probe and the launch invocation.

The daemon never starts Chrome. One dedicated profile means exactly one Chrome may own it, and a
second launch on a live profile either hangs or opens read-only — so supervision belongs to launchd
(and, in dev, to the person at the keyboard). What lives here is the probe that answers "is it
there", and the command to run when it is not.

The probe is the only network I/O in the browser layer: an HTTP GET of Chrome's own CDP version
endpoint on the loopback interface.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from selly_agent import paths

_PROBE_TIMEOUT_SEC = 2.0

# Chrome's macOS install location. The dev bring-up prints this command rather than running it.
_CHROME_MACOS = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Locks a SIGKILLed Chrome leaves behind. They make the next launch on the same profile hang, so the
# bring-up clears them — safe only because it has already established that no Chrome is answering.
SINGLETON_LOCKS = ("SingletonLock", "SingletonCookie", "SingletonSocket")


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
    for name in SINGLETON_LOCKS:
        lock = profile / name
        try:
            lock.unlink()
        except OSError:
            continue
        removed.append(name)
    return removed


def launch_command(port: int, *, chrome_bin: str = _CHROME_MACOS) -> list:
    """The argv for the warm Chrome: the agent's own profile, CDP open on the loopback interface.

    A dedicated profile is the point — the seller's everyday Chrome is never driven, and the
    marketplace sessions the agent uses persist here across restarts.
    """
    return [
        chrome_bin,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={paths.browser_profile_dir()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--restore-last-session",
        "--hide-crash-restore-bubble",
        "--window-position=80,80",
        "--window-size=1200,900",
    ]


def bring_up_hint(port: int) -> str:
    """What to tell a person whose Chrome is not running. Installing the launchd job that keeps it
    alive is the installer's work; this is the dev-mode instruction."""
    argv = launch_command(port)
    quoted = " ".join(f'"{part}"' if " " in part else part for part in argv)
    return f"the agent's Chrome is not running on port {port} — start it with:\n  {quoted}"
