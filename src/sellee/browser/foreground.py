"""Bring the agent's own Chrome window in front of the seller — and only that Chrome. macOS only.

Nothing here works on any other platform, and `is_supported` says so before a caller promises the
seller anything — a window that never comes forward is what this exists to prevent, so a setup
question or a "can't spot it?" hint about a raise that cannot happen is worse than silence.

The seller's personal Chrome is the same app bundle as the agent's, so anything that activates
"Google Chrome" by name (`open -a`, AppleScript's `activate`) can raise the wrong instance. The
one thing that distinguishes the agent's Chrome is its CDP port, so the raise is pid-targeted:
`lsof` names the process listening on that port, and that pid — nothing broader — is activated.

Activation goes through `osascript -l JavaScript` calling `NSRunningApplication` over the ObjC
bridge: a plain Cocoa call, not an Apple Event to another app, so it never trips an Automation or
Accessibility permission prompt mid-flow. Known limitation: activation does not de-miniaturize a
minimized window (the Dock icon highlights instead), which the connect flow's hint copy covers.

Every failure here — no lsof, no listener, a refused activation, a hung tool — reads as False,
never an exception: the callers print a "look for the window" hint on False, and a raise that
could break a sign-in would be worse than no raise at all. `raise_window` and `is_supported` are
the pair another platform's implementation lands behind.
"""

from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger(__name__)

_LSOF_TIMEOUT_SEC = 3.0
_OSASCRIPT_TIMEOUT_SEC = 5.0

# The only dynamic value interpolated is int(pid), re-validated at the call site — nothing lsof
# prints can reach this script unparsed. ActivateAllWindows so a multi-window Chrome brings all of
# its windows forward, not just the key one.
_MACOS_ACTIVATE_JXA = (
    'ObjC.import("AppKit");'
    "var app = $.NSRunningApplication.runningApplicationWithProcessIdentifier(%d);"
    'if (app.isNil()) throw "no process with that pid";'
    "app.activateWithOptions("
    "$.NSApplicationActivateAllWindows | $.NSApplicationActivateIgnoringOtherApps);"
)


def is_supported() -> bool:
    """Whether this OS can raise the agent's window at all — as opposed to a raise that was
    attempted and did not land, which is `raise_window` returning False."""
    return sys.platform == "darwin"


def raise_window(cdp_port: int) -> bool:
    """Bring the Chrome that owns `cdp_port` to the front. True only when the activation ran and
    reported success; False for every reason it could not (the caller hints, never errors)."""
    if not is_supported():
        return False
    pid = chrome_pid(cdp_port)
    if pid is None:
        return False
    return _activate_macos(pid)


def chrome_pid(cdp_port: int) -> int | None:
    """The pid listening on the CDP port — Chrome's main process — or None when there is no
    listener or the answer cannot be trusted (unparseable output is a None, not a guess)."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{cdp_port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=_LSOF_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("could not look up the Chrome pid on port %s: %s", cdp_port, exc)
        return None
    if result.returncode != 0:
        return None
    pids = []
    for token in result.stdout.split():
        try:
            pid = int(token)
        except ValueError:
            log.debug("unexpected lsof output for port %s: %r", cdp_port, result.stdout)
            return None
        if pid not in pids:
            pids.append(pid)
    if not pids:
        return None
    if len(pids) > 1:
        # Only one process can LISTEN on the port; extra rows are IPv4/IPv6 duplicates at most.
        log.debug("multiple pids on port %s (%s) — using the first", cdp_port, pids)
    return pids[0]


def _activate_macos(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", _MACOS_ACTIVATE_JXA % int(pid)],
            capture_output=True,
            text=True,
            timeout=_OSASCRIPT_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("could not activate Chrome pid %s: %s", pid, exc)
        return False
    if result.returncode != 0:
        log.debug("activating Chrome pid %s was refused: %s", pid, result.stderr.strip())
        return False
    return True
