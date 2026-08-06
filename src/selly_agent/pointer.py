"""Filesystem indirection: the `current` pointer, and the command shim that follows it.

Both exist so that nothing else has to know which version is live. The launcher, the supervised
job, the generated harness configs and the shim all name a path *through* `current`, and an update
changes what it resolves to — so the indirection has to be at the filesystem level, where every
consumer resolves it without being told.

POSIX gets symlinks. Windows gets a directory junction for the pointer, because a symlink there
needs either administrator rights or developer mode, and gets a small script for the shim, because
a shebang file is not executable. Junctions cannot be replaced in one step the way a symlink can,
so the Windows swap has a brief window with no pointer at all — the callers that swap do it while
the daemon is stopped, and the shim retries.
"""

from __future__ import annotations

import os
from pathlib import Path

_WINDOWS = os.name == "nt"

# The shim is a script on Windows, so its name carries an extension there. Read through paths, which
# stays the single authority on where things live; this is only the naming rule.
SHIM_SUFFIX = ".cmd" if _WINDOWS else ""

# What the Windows shim runs. The path it names goes through `current`, so it is re-resolved on
# every invocation — which is what makes an update invisible to whoever typed the command.
_CMD_SHIM = """\
@echo off
setlocal
set "SELLY_LAUNCHER={target}"
set /a SELLY_TRIES=0
:selly_wait
if exist "%SELLY_LAUNCHER%" goto selly_run
rem An update is mid-swap: the pointer is gone for a moment. Wait rather than fail — and keep
rem waiting a few rounds, because a scanner holding the old target can stretch the moment.
set /a SELLY_TRIES+=1
if %SELLY_TRIES% geq 5 goto selly_run
timeout /t 1 /nobreak >nul 2>&1
goto selly_wait
:selly_run
"{interpreter}" "%SELLY_LAUNCHER%" %*
"""


def _shim_encoding() -> str:
    """What the .cmd shim is written in.

    cmd.exe parses a batch file in the console code page, not the ANSI one a locale write would
    use. Where the two differ — an accented name under C:\\Users — the target spelled in one is
    not the target read in the other, and the shim's own `if exist` never matches it.
    """
    return "oem" if _WINDOWS else "utf-8"


def is_pointer(path) -> bool:
    """Whether `path` is one of our indirections rather than a real directory.

    A junction is not a symlink as far as `is_symlink` is concerned, so asking the wrong question
    on Windows reports a managed pointer as a directory somebody else put there.
    """
    target = Path(path)
    if target.is_symlink():
        return True
    return _WINDOWS and target.is_junction()


def read(path):
    """What the pointer resolves to, or None when it is not a pointer."""
    return Path(os.path.realpath(path)) if is_pointer(path) else None


def swap(pointer, target) -> Path:
    """Point `pointer` at `target`.

    On POSIX this is a rename over the old link, so the pointer is never absent. Windows has no
    equivalent for a junction — a directory cannot be renamed over an existing one — so the old
    junction is removed and a new one created, and for those few milliseconds the pointer does not
    exist. Removing a junction removes only the junction; what it pointed at is untouched.
    """
    pointer, target = Path(pointer), Path(target)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    if _WINDOWS:
        discard(pointer)
        _create_junction(pointer, target)
        return pointer
    staging = pointer.with_name(pointer.name + ".new")
    discard(staging)
    staging.symlink_to(target)
    os.replace(staging, pointer)
    return pointer


def discard(path) -> None:
    """Remove a pointer or a leftover file if one is there, following nothing.

    Following it is the mistake this exists to prevent: deleting *through* `current` would take
    the version it names with it.
    """
    path = Path(path)
    if _WINDOWS and path.is_junction():
        # A junction is a directory entry, so it goes with rmdir; its target is untouched.
        os.rmdir(path)
    elif path.is_symlink() or path.exists():
        path.unlink()


def _create_junction(pointer: Path, target: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(pointer))


# --- the command shim -------------------------------------------------------------------------


def write_shim(shim, launcher, interpreter) -> Path:
    """Put a runnable `shim` in place that hands `launcher` to `interpreter`.

    On POSIX the launcher's own shebang does that, so the shim is a link to it and this stays one
    filesystem operation. Windows needs a script, which means the interpreter is named rather than
    discovered — the launcher re-execs onto the version's own venv anyway, so naming a reasonable
    one here is enough to get that far.
    """
    shim, launcher = Path(shim), Path(launcher)
    shim.parent.mkdir(parents=True, exist_ok=True)
    staging = shim.with_name(shim.name + ".new")
    discard(staging)
    if _WINDOWS:
        body = _CMD_SHIM.format(target=launcher, interpreter=Path(interpreter))
        # Encoded here and written as bytes rather than through write_text: the code-page codecs
        # have no working incremental encoder, so a text-mode write refuses even pure ASCII.
        staging.write_bytes(body.encode(_shim_encoding()))
    else:
        staging.symlink_to(launcher)
    os.replace(staging, shim)
    return shim


def shim_target(shim):
    """What the shim will run, or None when it is unreadable or not ours to read."""
    shim = Path(shim)
    if shim.is_symlink():
        return Path(os.readlink(shim))
    if not _WINDOWS:
        return None
    try:
        for line in shim.read_bytes().decode(_shim_encoding()).splitlines():
            if line.startswith('set "SELLY_LAUNCHER='):
                return Path(line.split("=", 1)[1].rstrip('"'))
    except OSError:
        return None
    return None
