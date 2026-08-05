"""Spawning external programs portably.

Programs are resolved to full paths here rather than passed by bare name, because a bare name is
not spawnable everywhere: Windows process creation ignores PATHEXT, so `npx` — which is `npx.cmd`
there — fails to spawn even on a machine where the installer's `which`-based gates found it. That
made for the worst shape of bug available: setup passes, and the browser lane dies at runtime.
"""

from __future__ import annotations

import os
import shutil
import subprocess


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


def become(argv) -> int:
    """Hand this process's work to `argv` and do not come back — or the nearest thing available.

    POSIX replaces the process, so signals and the terminal behave exactly as if the person had run
    the program themselves; nothing after the call runs. Windows has no such thing: exec there
    starts a *new* process and lets this one return, which would drop the caller back at a prompt
    while a detached child owned their terminal. So the child is run to completion instead and its
    exit status becomes ours, which costs one lingering parent process and keeps the terminal sane.
    """
    resolved = resolve(argv)
    if os.name != "nt":
        os.execv(resolved[0], resolved)  # never returns
    return subprocess.run(resolved).returncode  # noqa: S603 — argv is composed by the caller


def detached_flags() -> dict:
    """Popen keywords that put a child in its own group, so it can be stopped as a unit.

    POSIX gets a new session, which makes the child a group leader and its whole tree signalable
    at once. Windows has no session, so the nearest thing is a new process group plus no console:
    without CREATE_NO_WINDOW a background job flashes a console window on every pass.
    """
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def survives_us_flags() -> dict:
    """Popen keywords for a child meant to outlive this process — the agent's Chrome.

    Chrome stays up across a normal daemon exit or crash, so it must not be part of a group the
    daemon's own shutdown would signal. This is best-effort, not absolute: the forced kill of a
    wedged daemon walks its child tree and takes a Chrome that daemon spawned — accepted, because
    it is the agent's own Chrome on a dedicated profile, and the next launch recovers (sessions
    persist on disk; stale profile locks are cleared).
    """
    if os.name == "nt":
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}
