"""The versioned layout: stage a tree into `versions/<v>`, swap `current`, own the PATH shim.

Install and update are the same operation here — an install is an update into an empty
`versions/` — so both go through one module and the default install exercises, on every machine,
exactly the path an update will later take.

Two invariants shape everything below. `current` is *always* a pointer we own — a symlink, or a
junction where symlinks need privileges: a real directory there is someone else's (or a
half-finished copy), and replacing it silently is how an install eats a tree it did not create, so
it is refused with a remediation instead. And a version directory becomes visible only by rename:
the copy lands beside its destination and is moved into place in one step, so a crash mid-copy
leaves a stray `.tmp` and a still-working install rather than a half-populated version that
`current` already points at.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from selly_agent import paths, pointer
from selly_agent.installer import runtime

# What a version directory holds: the launcher and the package (whose migrations, registries and
# skills ship as package data inside it), plus the three files describing the runtime — which are
# what let a version build its own venv, and so what makes it self-contained enough to roll back
# to. A checkout's .git, tests and dev Makefile are not part of a running install. `make dist`
# packs this same set, so installing from a checkout and from a release tarball match.
VERSION_DIRS = ("bin", "src")
VERSION_FILES = (
    "setup",
    # The Windows front door ships in every version too, so a tree installed from a release can be
    # re-run there — and so `update` stages a version that is runnable on either platform.
    "setup.ps1",
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
)

# .venv holds absolute paths, so a dev checkout's would be wrong here. Every version builds its own.
_IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".venv")

# How many installed versions to keep. Enough that a rollback has somewhere to go and the one
# before it is still around for comparison; not so many that the data root grows without bound.
VERSIONS_KEPT = 3

# The PATH block setup offers to add, fenced by markers so a re-run can find it, leave it alone,
# and an uninstall can remove exactly what was added and nothing around it.
RC_MARKER_START = "# >>> selly-agent >>>"
RC_MARKER_END = "# <<< selly-agent <<<"
RC_BLOCK_BODY = 'export PATH="$HOME/.local/bin:$PATH"'

# What to tell a fish user instead of editing anything. `fish_add_path` is fish's own documented
# way (3.2+), it is idempotent, and it persists by itself — so it is one command to run rather
# than a config edit to make, which is the right shape for a shell we have decided not to touch.
FISH_COMMAND = "fish_add_path ~/.local/bin"
FISH_CONFIG_LINE = "set -gx PATH $HOME/.local/bin $PATH"


class LayoutError(Exception):
    """The layout is not in a state we may write to. Carries the remediation."""

    def __init__(self, message: str, fix: str = ""):
        super().__init__(message)
        self.message = message
        self.fix = fix


def source_tree() -> Path:
    """The tree this code is running out of: a checkout during development, or the version
    directory of an install. What setup stages and what `daemon install` falls back to."""
    return Path(__file__).resolve().parents[3]


# --- reading the layout -------------------------------------------------------------------


def install_interpreter() -> Path:
    """The interpreter this install runs on, for whoever has to name one: the supervised job and
    the Windows shim.

    Named through `current` rather than the version behind it, so the name stays true across an
    update. Falls back to whatever is running us when there is no venv — a checkout pointed at by
    `./setup --dev` before bootstrapping — because naming an interpreter that does not exist gives
    a job that fails to start with nothing explaining why, and the launcher re-execs onto the venv
    by itself once there is one.
    """
    interpreter = paths.venv_python(paths.current())
    return interpreter if interpreter.exists() else Path(os.path.realpath(sys.executable))


def supervised_interpreter() -> Path:
    """The interpreter to name in the background job's definition.

    install_interpreter answers for a person at a terminal; a job wants the same interpreter
    without a console attached, which on Windows is a different executable beside it.
    """
    interpreter = paths.venv_windowless_python(paths.current())
    return interpreter if interpreter.exists() else install_interpreter()


def current_target():
    """What `current` points at, or None when it does not exist. Never follows further."""
    return pointer.read(paths.current())


def current_version():
    """The installed version's name when `current` points into `versions/`, else None.

    None is the dev answer: a checkout symlinked in place has no version to name, which is
    exactly what makes `update` refuse it.
    """
    target = current_target()
    if target is None:
        return None
    versions = paths.versions_dir().resolve()
    try:
        relative = target.relative_to(versions)
    except ValueError:
        return None
    return relative.parts[0] if relative.parts else None


def is_dev_install() -> bool:
    """Whether `current` points somewhere other than a version directory — a developer's
    checkout. The marker that keeps `update` from swapping a working tree out from under
    someone, and keeps the update probe from nagging them about every release."""
    return current_target() is not None and current_version() is None


def installed_versions() -> list:
    """Version directories, oldest install first. Ordered by when they were put here rather than
    by parsing their names: "installed most recently" is what retention and rollback mean, and it
    stays true for dev builds that all call themselves the same thing."""
    root = paths.versions_dir()
    if not root.is_dir():
        return []
    found = [p for p in root.iterdir() if p.is_dir() and not p.name.endswith((".tmp", ".old"))]
    return sorted(found, key=lambda p: p.stat().st_mtime)


def previous_version():
    """The most recently installed version that is not the live one — where a rollback goes."""
    live = current_version()
    others = [p for p in installed_versions() if p.name != live]
    return others[-1] if others else None


# --- writing the layout -------------------------------------------------------------------


def _guard_current_is_ours() -> None:
    current = paths.current()
    if current.exists() and not pointer.is_pointer(current):
        raise LayoutError(
            f"{current} is a real directory, not the pointer selly-agent manages",
            f"Move or remove it (it was not created by selly-agent), then re-run:\n"
            f"  mv {current} {current}.bak",
        )


def _fsync_dir(path: Path) -> None:
    """Flush the directory entry so the rename that follows survives a crash. This makes the
    *switch* durable, not every byte of every copied file — a torn copy is discarded by the
    rename never happening, which is the direction that is safe to fail in."""
    if os.name == "nt":
        # Windows has no fd for a directory: the CRT refuses to open one, so this would raise
        # PermissionError rather than syncing anything. The rename below is still atomic there
        # (MoveFileEx), and NTFS journals the metadata itself, so there is nothing to flush by
        # hand — the durability this function buys on POSIX is already the filesystem's.
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:
        pass  # some filesystems refuse fsync on a directory; the rename is still atomic
    finally:
        os.close(fd)


def stage_version(tree, version: str) -> Path:
    """Copy the release contents of `tree` into `versions/<version>`, atomically.

    The copy goes to a sibling `.tmp` and is renamed in, so `versions/<version>` either does not
    exist or is complete. Re-staging a version that already exists (every dev build calls itself
    the same thing) swaps the new tree in and deletes the old one afterwards.
    """
    tree = Path(tree)
    root = paths.versions_dir()
    root.mkdir(parents=True, exist_ok=True)
    dest = root / version
    staged = root / f"{version}.tmp"

    # Sweep any `.old` left by a crash between the two renames below — `installed_versions`
    # hides them, so nothing else would ever notice they are there.
    for leftover in root.glob("*.old"):
        shutil.rmtree(leftover, ignore_errors=True)
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir()
    for name in VERSION_DIRS:
        source = tree / name
        if not source.is_dir():
            raise LayoutError(
                f"{tree} does not look like a selly-agent tree ({name}/ is missing)",
                "Run ./setup from an unpacked release or a checkout.",
            )
        shutil.copytree(source, staged / name, ignore=_IGNORED)
    for name in VERSION_FILES:
        source = tree / name
        if source.is_file():
            shutil.copy2(source, staged / name)
    _fsync_dir(staged)

    displaced = root / f"{version}.old"
    if dest.exists():
        if displaced.exists():
            shutil.rmtree(displaced)
        os.replace(dest, displaced)
    os.replace(staged, dest)
    if displaced.exists():
        shutil.rmtree(displaced)
    return dest


def swap_current(target) -> Path:
    """Point `current` at `target`.

    The supervised job, the shim and every generated harness config resolve through this pointer,
    so the swap is the whole of an update as far as they are concerned. Where the platform allows
    it the change is a rename and the pointer is never absent; where it does not, callers swap with
    the daemon stopped (see pointer.swap).
    """
    _guard_current_is_ours()
    return pointer.swap(paths.current(), target)


def ensure_current(checkout) -> None:
    """Make sure `current` resolves, without ever overruling a real install.

    Called by `daemon install`, which can run in three worlds: from a checkout nobody has set up
    (point at the checkout — the dev loop), from an install setup or update just materialized
    (leave it exactly alone — re-pointing it at the tree this code happens to live in is how an
    update undoes its own swap), or over a directory we do not own (refuse).
    """
    paths.ensure_runtime_dirs()
    _guard_current_is_ours()
    if pointer.is_pointer(paths.current()):
        return
    swap_current(Path(checkout).resolve())


def install_dev(checkout) -> Path:
    """`./setup --dev`: point `current` straight at the working tree, so an edit is live on the
    next daemon restart with no copy in between."""
    paths.ensure_runtime_dirs()
    return swap_current(Path(checkout).resolve())


def install_version(tree, version: str, *, provision=None) -> Path:
    """The default install and every update: stage the tree as a version, give it its
    dependencies, then make it current.

    The venv is built after the rename into versions/<v> and before the swap, for two separate
    reasons. After, because a venv records absolute paths and one built in the staging directory
    would describe the wrong place. Before, because a version without dependencies must never
    become the live one: a failure here leaves the previous version running, which is what makes
    a failed update recoverable.
    """
    paths.ensure_runtime_dirs()
    _guard_current_is_ours()
    dest = stage_version(tree, version)
    (provision if provision is not None else runtime.provision)(dest)
    swap_current(dest)
    return dest


def prune_versions(keep: int = VERSIONS_KEPT) -> list:
    """Delete all but the newest `keep` versions, never touching the live one. Returns what went."""
    live = current_version()
    candidates = [p for p in installed_versions() if p.name != live]
    # The live version occupies one of the `keep` slots when there is one; with no live version
    # (nothing installed yet) all `keep` slots are available to the rest.
    room = max(0, keep - (1 if live else 0))
    doomed = candidates[: max(0, len(candidates) - room)]
    removed = []
    for path in doomed:
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path.name)
    return removed


# --- the ~/.local/bin shim ------------------------------------------------------------------


def shim_target() -> Path:
    """What the shim points at: the launcher through `current`, never at a version directly, so
    the command a person types keeps working across every update without being rewritten."""
    return paths.current() / "bin" / "selly-agent"


def _shim_is_ours(shim: Path) -> bool:
    """Whether the name at the shim path is one we installed.

    Two ways to be ours, because a dev install has neither the same recorded target nor a resolved
    one under the data root as a versioned install: what it names is exactly what `install_shim`
    writes, or it resolves inside our data root.
    """
    named = pointer.shim_target(shim)
    if named is None:
        return False
    if named == shim_target():
        return True
    resolved = Path(os.path.realpath(named))
    root = paths.data_root().resolve()
    return root == resolved or root in resolved.parents


def install_shim() -> Path:
    """Put `selly-agent` on the user's PATH, pointing at the launcher inside `current`."""
    shim = paths.shim_path()
    target = shim_target()
    shim.parent.mkdir(parents=True, exist_ok=True)
    if (shim.is_symlink() or shim.exists()) and not _shim_is_ours(shim):
        # Taking a name we did not create is the mirror of the care `remove_shim` takes not to
        # delete one: an install that quietly replaces someone else's command cannot give it back.
        raise LayoutError(
            f"{shim} already exists and was not created by selly-agent",
            "Something else owns that name. Move it aside and re-run ./setup.",
        )
    return pointer.write_shim(shim, target, install_interpreter())


def remove_shim() -> bool:
    """Remove the shim, but only when it is one we installed."""
    shim = paths.shim_path()
    if not _shim_is_ours(shim):
        return False
    shim.unlink()
    return True


# --- PATH ---------------------------------------------------------------------------------


def user_bin_on_path(path_value=None) -> bool:
    """Whether `~/.local/bin` is already on the PATH the invoking shell hands us."""
    raw = os.environ.get("PATH", "") if path_value is None else path_value
    target = paths.user_bin_dir()
    for entry in raw.split(os.pathsep):
        if entry and Path(entry) == target:
            return True
    return False


# --- the user PATH on Windows ----------------------------------------------------------------

# Where a user's own PATH lives. Per-user, so no elevation is needed and nothing machine-wide is
# touched. A separate value from the machine PATH, which the session concatenates with this one.
_USER_ENVIRONMENT_KEY = "Environment"

# Windows' own rules, written out rather than taken from this host: the value being edited is a
# Windows registry value whichever machine reads the code, so borrowing os.pathsep and normcase
# would make the logic mean different things in a test and in production.
_WINDOWS_PATH_SEP = ";"


def _same_directory(left: str, right: str) -> bool:
    """Whether two PATH entries name one directory, by Windows' rules: case-insensitive, and a
    trailing separator is not a difference. Without this an install appends a second copy of the
    same directory every time somebody's PATH happens to spell it differently."""
    return left.strip().rstrip("\\/").lower() == right.strip().rstrip("\\/").lower()


def _read_user_path() -> str:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _USER_ENVIRONMENT_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, "Path")
            return value or ""
    except FileNotFoundError:
        return ""  # an account that has never had a user PATH of its own


def _write_user_path(value: str) -> None:
    import winreg

    key_args = (winreg.HKEY_CURRENT_USER, _USER_ENVIRONMENT_KEY, 0, winreg.KEY_WRITE)
    with winreg.CreateKeyEx(*key_args) as key:
        # REG_EXPAND_SZ, because a user PATH conventionally holds entries like %USERPROFILE%\... and
        # writing it as a plain string would stop those from expanding for everyone else's entries.
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, value)
    _broadcast_environment_change()


def _broadcast_environment_change() -> None:
    """Tell running programs the environment changed, so a new terminal sees the entry.

    Without this the registry holds the new PATH but nothing reads it until the next sign-in, and
    the seller is told to reopen a terminal that would not have helped. Explorer is what propagates
    it to the terminals it launches. Best-effort: a failure here costs a sign-in, not the install.
    """
    import ctypes

    HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002
    try:
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", SMTO_ABORTIFHUNG, 1000, None
        )
    except OSError:
        pass


def user_path_entries(value=None) -> list:
    raw = _read_user_path() if value is None else value
    return [entry for entry in raw.split(_WINDOWS_PATH_SEP) if entry.strip()]


def add_user_path_entry() -> bool:
    """Put our bin dir on the account's own PATH. Answers whether anything was written.

    Appended rather than prepended: this is not a directory that should shadow the seller's own
    tools, and the only thing in it is ours.
    """
    target = str(paths.user_bin_dir())
    existing = user_path_entries()
    if any(_same_directory(entry, target) for entry in existing):
        return False
    _write_user_path(_WINDOWS_PATH_SEP.join([*existing, target]))
    return True


def remove_user_path_entry() -> bool:
    """Take our bin dir off the account's PATH, leaving every other entry exactly as it was."""
    target = str(paths.user_bin_dir())
    existing = user_path_entries()
    kept = [entry for entry in existing if not _same_directory(entry, target)]
    if len(kept) == len(existing):
        return False
    _write_user_path(_WINDOWS_PATH_SEP.join(kept))
    return True


def rc_block() -> str:
    return f"{RC_MARKER_START}\n{RC_BLOCK_BODY}\n{RC_MARKER_END}\n"


def rc_block_present(text: str) -> bool:
    return RC_MARKER_START in (text or "")


def add_rc_block(rc_path) -> bool:
    """Append the marked PATH block to a shell rc file. Idempotent: a second run finds its own
    marker and changes nothing. Returns whether anything was written."""
    rc_path = Path(rc_path)
    existing = rc_path.read_text() if rc_path.is_file() else ""
    if rc_block_present(existing):
        return False
    rc_path.parent.mkdir(parents=True, exist_ok=True)
    separator = "" if not existing or existing.endswith("\n") else "\n"
    rc_path.write_text(existing + separator + "\n" + rc_block())
    return True


def strip_rc_block(text: str) -> str:
    """Remove the marked block from rc-file text, leaving everything around it untouched.

    An unterminated block — someone edited the file and lost the closing fence — leaves the text
    exactly as it was. Skipping to end-of-file instead would silently delete everything the
    person wrote after our block, which is far worse than leaving three lines behind.
    """
    lines = (text or "").splitlines(keepends=True)
    out, skipping, closed = [], False, True
    for line in lines:
        if line.strip() == RC_MARKER_START:
            skipping, closed = True, False
            # Drop one blank line we ourselves inserted before the block, so repeated
            # add/remove cycles do not accumulate whitespace.
            if out and out[-1].strip() == "":
                out.pop()
            continue
        if skipping:
            if line.strip() == RC_MARKER_END:
                skipping, closed = False, True
            continue
        out.append(line)
    return text if not closed else "".join(out)


def remove_rc_block(rc_path) -> bool:
    """Remove our block from an rc file, answering whether anything actually changed.

    False covers both "it was never there" and "it is there but unterminated, so removing it
    safely is not possible" — in neither case has the file been touched, and saying it was
    would be a claim the caller then repeats to the seller.
    """
    rc_path = Path(rc_path)
    if not rc_path.is_file():
        return False
    text = rc_path.read_text()
    if not rc_block_present(text):
        return False
    stripped = strip_rc_block(text)
    if stripped == text:
        return False
    rc_path.write_text(stripped)
    return True


def shell_name(shell=None) -> str:
    """The login shell's name — `$SHELL` basenamed, or "" when it says nothing.

    `$SHELL` is the shell the account is configured with, not necessarily the one being typed in
    right now (run bash inside zsh and this still says zsh). That is the same trade uv and rustup
    make, and for the question being asked — which startup file will run next login — the login
    shell is the right one to ask about.
    """
    resolved = shell if shell is not None else os.environ.get("SHELL", "")
    return os.path.basename(resolved or "")


def shell_rc_target(shell=None) -> Path | None:
    """The rc file to offer to edit, or None for a shell whose config we leave alone."""
    return paths.shell_rc_path(shell if shell is not None else os.environ.get("SHELL", ""))


def rc_candidates() -> list:
    """Every rc file an install might have written the PATH block to."""
    return paths.shell_rc_candidates()


# --- the mutation preview -------------------------------------------------------------------


def layout_preview(platform=None) -> list:
    """Every location an install writes to, rendered from the path authority itself so the
    preview cannot drift from what actually happens."""
    rows = [
        (
            paths.data_root(),
            "code (versions/, each with its own dependencies), the Python toolchain, "
            "the database, photos, the browser profile",
        ),
        (paths.state_dir(), "logs, transcripts, database backups"),
        (paths.config_dir(), "config.json and secrets (0600)"),
        (paths.cache_dir(), "downloaded release archives"),
        (paths.shim_path(), "the `selly-agent` command"),
        (
            paths.launch_agents_dir(platform=platform),
            "the background job (only if you choose start-at-login)",
        ),
    ]
    width = max(len(str(location)) for location, _ in rows)
    return [f"  {str(location).ljust(width)}   {what}" for location, what in rows]
