"""`selly-agent uninstall` — take it all back off, and say exactly what went.

Removal only ever touches what an install put there: the launchd job is removed only if it
carries our marker, the shim only if it points into our data root, and the shell rc block only
between its own fences. Anything sharing a name that we did not write is left alone and named,
because the alternative — deleting a stranger's file that happened to match — is unrecoverable.

`--preserve-data` keeps the database, the photos and the browser profile, and *also* keeps the
config directory's secrets. That pairing is not an oversight: the carousell.ai key is the rail
identity every listing was created under, so a database kept without it is a set of listings
nobody can act on again.
"""

from __future__ import annotations

import shutil

from selly_agent import paths, pointer, supervisor
from selly_agent.installer import materialize
from selly_agent.installer.ui import Abort, Ui
from selly_agent.platform import get_platform


def run(args) -> int:
    ui = Ui(assume_yes=getattr(args, "yes", False))
    try:
        return _run(args, ui)
    except Abort as exc:
        ui.fatal(exc)
        return 1


def _run(args, ui: Ui) -> int:
    preserve = bool(getattr(args, "preserve_data", False))
    platform = get_platform()

    ui.step("Uninstalling selly-agent from this Mac")
    for line in _plan(preserve):
        ui.say(line)
    # Deleting someone's data defaults to no, and `--yes` is read as the consent itself rather
    # than as "take the default" — which for this question is the opposite of what it means.
    if not ui.assume_yes and not ui.confirm("Go ahead?", default=False):
        ui.say("Left everything alone.")
        return 0

    ui.step("Uninstalling")
    ui.say("stopping the background worker")
    supervisor.uninstall(platform=platform)

    if materialize.remove_shim():
        ui.say(f"removed {paths.shim_path()}")
    for rc_file in materialize.rc_candidates():
        # Every shell we might have written to, not just the one running now: install under zsh
        # and uninstall from bash, and checking only the current shell leaves the block behind.
        if materialize.remove_rc_block(rc_file):
            ui.say(f"removed the PATH line from {rc_file}")

    for target in _roots_to_remove(preserve):
        _remove(target)
        ui.say(f"removed {target}")
    ui.say("")
    ui.say("Done.")
    return 0


def _plan(preserve: bool) -> list:
    lines = [
        "• stop and unregister the background worker",
        f"• remove {paths.shim_path()} and PATH changes, if we added any",
        f"• remove {paths.versions_dir()} (the installed code)",
        f"• remove {paths.state_dir()} (logs, transcripts, backups)",
    ]
    if preserve:
        lines.append(f"• KEEP {paths.data_root()} (database, photos, browser profile)")
        lines.append(f"• KEEP {paths.config_dir()} (config and secrets)")
    else:
        lines.append(f"• remove {paths.data_root()} (database, photos, browser profile)")
        lines.append(f"• remove {paths.config_dir()} (config and secrets)")
        lines.append(f"• remove {paths.cache_dir()} (cache)")
    return lines


def _roots_to_remove(preserve: bool) -> list:
    if preserve:
        # The version trees and the pointer go; the data beside them stays.
        return [paths.versions_dir(), paths.current(), paths.state_dir(), paths.cache_dir()]
    return [paths.data_root(), paths.state_dir(), paths.config_dir(), paths.cache_dir()]


def _remove(target) -> None:
    # A pointer is removed, never followed: deleting through `current` would take the version it
    # names with it. On Windows that pointer is a junction, which is a directory entry.
    if pointer.is_pointer(target):
        pointer.discard(target)
    elif target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
