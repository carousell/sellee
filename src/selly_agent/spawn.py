"""Spawning external programs portably.

Programs are resolved to full paths here rather than passed by bare name, because a bare name is
not spawnable everywhere: Windows process creation ignores PATHEXT, so `npx` — which is `npx.cmd`
there — fails to spawn even on a machine where the installer's `which`-based gates found it. That
made for the worst shape of bug available: setup passes, and the browser lane dies at runtime.
"""

from __future__ import annotations

import shutil


def resolve(argv) -> list:
    """`argv` with its program resolved to a full path; unchanged when nothing matches.

    Left alone rather than raised on, so an unresolvable program still reaches the caller's own
    "not installed" error instead of a bare OSError from the spawn.
    """
    parts = [str(arg) for arg in argv]
    if not parts:
        return parts
    found = shutil.which(parts[0])
    return [found, *parts[1:]] if found else parts
