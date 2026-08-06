"""Which OS this is — asked once, spelled one way.

Before this existed the tree asked in three idioms (`os.name == "nt"`, `os.name != "nt"`,
`sys.platform == "win32"`), sometimes two of them in the same module. They agree, which is what
made the inconsistency survive: nothing was ever wrong, so nothing forced a decision.

This is deliberately not a general capability layer. It answers the one question the platform
owners ask, and it is a *policed* way to ask it: the portability guard treats a call here exactly
as it treats `os.name`, so a module that is not an owner still cannot branch on the host. A helper
the guard did not know about would be a hole in that rule rather than an improvement to it.

Functions rather than constants so a test can patch one place, and so nothing freezes an answer at
import time that a caller might reasonably expect to re-read.

`bin/selly-agent` cannot use this: the launcher runs before the venv exists and so imports nothing
of ours. Its own checks stay inline, which is the one place two spellings is the lesser evil.
"""

from __future__ import annotations

import os
import sys


def windows() -> bool:
    """Windows. `os.name` rather than `sys.platform`: it is the distinction that matters here
    (one process model, one path syntax) and it holds on any Python that runs there."""
    return os.name == "nt"


def macos() -> bool:
    """macOS. Needs `sys.platform` — `os.name` is "posix" here, the same as Linux."""
    return sys.platform == "darwin"


def linux() -> bool:
    return sys.platform.startswith("linux")


def name() -> str:
    """A stable name for this host, for keying per-OS tables.

    Its own vocabulary rather than `sys.platform`'s, so a table reads as the platforms we support
    ("windows", "macos") instead of as the values CPython happens to report ("win32", "darwin").
    """
    if windows():
        return "windows"
    if macos():
        return "macos"
    if linux():
        return "linux"
    return sys.platform
