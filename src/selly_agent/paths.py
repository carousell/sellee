"""The one path authority.

Every filesystem location the daemon uses is resolved here, and only here — this is the
structural defense against the stale-clone class of bug, where a generator writes to a
location the running daemon never reads. XDG overrides are honored (they are also the test
seam: point $XDG_*_HOME at a tmpdir). Nothing else in the package may reach for the home
directory or an XDG variable; a guard test enforces that.

Paths are resolved at call time, not import time, so an XDG override set by a test (or the
environment) always takes effect.
"""

from __future__ import annotations

import os
from pathlib import Path

from selly_agent import host, pointer
from selly_agent.platform import get_platform

APP = "selly-agent"

# Shipped package assets — the marketplace/scam registries, the web tail page (the migration SQL
# and skill markdown resolve the same way from their own modules). They live beside the code, so
# they resolve package-relative and never through the XDG roots below: those locate user state,
# which an asset is not.
PACKAGE_DATA_DIR = Path(__file__).resolve().parent / "data"


def _home() -> Path:
    return Path.home()


def _local_app_data() -> Path:
    """The Windows per-user, per-machine application directory.

    Local rather than roaming, deliberately and permanently: a roaming profile is copied between
    machines and can be redirected onto a network share or a synced folder, and everything under
    these roots is either machine-specific or must not leave the machine at all — the browser
    profile holding live marketplace sessions, SQLite databases in WAL mode, and the secrets.
    """
    return Path(os.environ.get("LOCALAPPDATA") or _home() / "AppData" / "Local")


def local_app_data() -> Path:
    """Windows' per-user application directory, for a caller looking for *someone else's* install
    under it — Chrome's, which can be per-user. Exposed so that this module stays the only reader
    of the variable, which is what the path-authority guard is protecting."""
    return _local_app_data()


def _root(var: str, xdg_rel: str, windows_leaf: str) -> Path:
    """A root directory: the XDG-style override if set, else the OS convention.

    The override comes first on every platform. It is how a power user relocates state, and it is
    the test seam — a suite that could not redirect these would be writing into the real profile.

    Windows has no equivalent of the XDG split, so the four roots become four subtrees of one
    %LOCALAPPDATA% directory rather than four locations. That keeps a single tree to install,
    inspect and remove, and keeps the code below identical on every platform.
    """
    override = os.environ.get(var)
    if override:
        return Path(override) / APP
    if host.windows():
        return _local_app_data() / APP / windows_leaf
    return _home() / xdg_rel / APP


_XDG_VARS = ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME")


def supervised_env_base() -> dict:
    """The environment a supervised job needs before it can locate anything at all.

    A job inherits nothing from the shell that installed it, so what it must be handed is decided
    here with the rest of the "where do things live" rules rather than at the call site.

    POSIX needs the home directory, because npm keeps its cache and config under it and a warm run
    should land in the cache the worker reads. Windows needs that plus the variables process
    creation itself consults: without SystemRoot a child cannot be started at all, and without
    PATHEXT a bare `npx` never resolves to `npx.cmd`. Those come from this process, where the
    machine is the authority — the home directory does not, so that a test can redirect it.
    """
    if not host.windows():
        return {"HOME": str(_home())}
    passthrough = ("SystemRoot", "PATHEXT", "COMSPEC", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP")
    env = {"USERPROFILE": str(_home())}
    env.update({name: os.environ[name] for name in passthrough if os.environ.get(name)})
    return env


def xdg_overrides() -> dict:
    """The XDG overrides active in this process — for handing on to a supervised job.

    A launchd (or systemd) job does not inherit the shell's environment. Without these pinned
    into the job definition, an install run under overrides provisions its state in one place
    and boots a daemon that resolves every root somewhere else — same code, different world.
    """
    return {var: os.environ[var] for var in _XDG_VARS if os.environ.get(var)}


# --- XDG roots -----------------------------------------------------------------------------


def data_root() -> Path:
    """Immutable installs + business data live here (never pruned)."""
    return _root("XDG_DATA_HOME", ".local/share", "share")


def state_dir() -> Path:
    """Transcripts, DB backups, logs — prunable by definition; safe to delete wholesale."""
    return _root("XDG_STATE_HOME", ".local/state", "state")


def config_dir() -> Path:
    """config.json + secrets (0700 where the OS enforces it)."""
    return _root("XDG_CONFIG_HOME", ".config", "config")


def cache_dir() -> Path:
    """Downloaded release tarballs (regenerable)."""
    return _root("XDG_CACHE_HOME", ".cache", "cache")


# --- data_root children --------------------------------------------------------------------


def versions_dir() -> Path:
    return data_root() / "versions"


def current() -> Path:
    """The swap point: a pointer into versions/<v> (dev mode: into the checkout)."""
    return data_root() / "current"


def data_dir() -> Path:
    return data_root() / "data"


def selly_db() -> Path:
    """Business data — migrated and snapshotted before migrations."""
    return data_dir() / "selly.db"


def media_dir() -> Path:
    """Inbound channel media (photo bursts) downloaded by the poller before any LLM sees them —
    business data, never pruned."""
    return data_root() / "media"


def browser_profile_dir() -> Path:
    """The dedicated Chrome profile the agent drives — never the seller's everyday Chrome.

    The marketplace logins live here, so it is business data: deleting it means logging in to every
    market again.
    """
    return data_root() / "browser-profile"


def tools_dir() -> Path:
    """Third-party binaries we fetch and own, kept out of every directory somebody else uses."""
    return data_root() / "tools"


def uv_path() -> Path:
    """Our own copy of uv, invoked by absolute path.

    Deliberately not ~/.local/bin: a user may have their own uv there, and taking that name is not
    ours to do. Under the data root, an uninstall removes it by removing that root.
    """
    return tools_dir() / ("uv.exe" if host.windows() else "uv")


def venv_dir(tree: Path) -> Path:
    """The dependency venv belonging to a tree — a version directory, or a dev checkout."""
    return Path(tree) / ".venv"


def venv_python(tree: Path) -> Path:
    """The interpreter inside a tree's venv: what the launcher re-execs onto and what the
    supervised job's definition names.

    bin/ vs Scripts/ is virtual-environment layout, not host integration, which is why it is
    answered here and not behind the platform seam — that seam refuses an unsupported host, and
    this has to resolve while a runtime is still being established.
    """
    scripts = "Scripts" if host.windows() else "bin"
    name = "python.exe" if host.windows() else "python"
    return venv_dir(tree) / scripts / name


def venv_windowless_python(tree: Path) -> Path:
    """The interpreter for a job that must not own a console — pythonw.exe on Windows.

    Started with the ordinary interpreter, a supervised daemon flashes a console window onto the
    desktop; with a keep-alive that starts it every few minutes, that is a window every few
    minutes. Nothing else differs. POSIX makes no such distinction.
    """
    if not host.windows():
        return venv_python(tree)
    return venv_dir(tree) / "Scripts" / "pythonw.exe"


# --- state_dir children --------------------------------------------------------------------


def events_db() -> Path:
    """Event/transcript store — prunable; deletable without data loss; recreated on startup."""
    return state_dir() / "events.db"


def backups_dir() -> Path:
    return state_dir() / "backups"


def logs_dir() -> Path:
    return state_dir() / "logs"


def heartbeat_path() -> Path:
    return state_dir() / "daemon.heartbeat.json"


def browser_output_dir() -> Path:
    """Where the Playwright MCP server writes its own output files.

    It saves a page snapshot per navigation, so it needs a directory we choose: left to itself it
    writes into whatever directory the process started in, which for the daemon is arbitrary and for
    a developer is their checkout. Those snapshots are page content — buyer
    messages, and on a composer page the seller's own saved address — so they belong under the
    prunable state tree and nowhere near a repo.
    """
    return state_dir() / "browser-output"


def passes_dir() -> Path:
    """Ephemeral per-pass workspaces, swept on pass end.

    Generated harness config, plus whatever a pass has to be handed as a real file — a browser
    publish stages the item's photos here, because the browser's upload reads only its own
    workspace roots.
    """
    return state_dir() / "passes"


def pass_workspace_dir(pass_id: str) -> Path:
    return passes_dir() / pass_id


def pass_stderr_log(pass_id: str) -> Path:
    return logs_dir() / f"pass-{pass_id}.stderr.log"


def lock_path() -> Path:
    return state_dir() / "daemon.lock"


# --- config_dir children -------------------------------------------------------------------


def config_path() -> Path:
    return config_dir() / "config.json"


def mcp_token_path() -> Path:
    """The persistent attended-session bearer token (a 0600 secret file)."""
    return config_dir() / "mcp_token"


def carousell_ai_api_key_path() -> Path:
    """The provisioned carousell.ai guest API key (a 0600 secret file)."""
    return config_dir() / "carousell_ai_api_key"


def telegram_bot_token_path() -> Path:
    """The bound Telegram bot token (a 0600 secret file, written by the connect flow)."""
    return config_dir() / "telegram_bot_token"


# --- platform-owned -----------------------------------------------------------------------


def home_dir() -> Path:
    """The home directory, for a caller that has to hand `HOME` to a child process it builds the
    environment for. Home resolution is this module's business, so it is asked here rather than
    read off the environment at the call site."""
    return _home()


def user_path(raw: str) -> Path:
    """A path a person typed, with a leading `~` expanded. Home expansion is this module's
    business, so callers hand the raw string here rather than reaching for the home directory
    themselves."""
    return Path(raw).expanduser()


def claude_bin_candidates() -> list[Path]:
    """Conventional user install locations for the `claude` CLI, resolved here so the pass
    runner never reaches for the home directory itself. PATH is searched separately by the
    caller (shutil.which); these cover the common non-PATH installs."""
    home = _home()
    if host.windows():
        # Where Claude Code's own Windows installer puts it, with and without the extension a
        # bare name would need PATHEXT to find.
        return [
            home / ".local" / "bin" / "claude.exe",
            home / ".local" / "bin" / "claude.cmd",
            home / ".claude" / "local" / "claude.exe",
        ]
    return [
        home / ".local" / "bin" / "claude",
        home / ".claude" / "local" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/opt/homebrew/bin/claude"),
    ]


def user_bin_dir() -> Path:
    """~/.local/bin — where the entry-point shim lands. A PATH convention rather than an XDG
    data home, so it is home-relative and not overridable.

    The same location on Windows, which has no convention of its own for a user-level command
    directory — and which is where Claude Code installs itself, so on a machine that passed the
    claude gate this is very likely already on PATH.
    """
    return _home() / ".local" / "bin"


def shim_path() -> Path:
    """The user-facing `selly-agent` command, resolving through `current` so it survives updates.
    Carries an extension where the OS needs one to consider a file runnable."""
    return user_bin_dir() / f"{APP}{pointer.SHIM_SUFFIX}"


def shell_rc_path(shell: str) -> Path | None:
    """The rc file a PATH export belongs in for this login shell, or None for a shell we do not
    write config for.

    Only the two we can be right about: zsh reads ~/.zshrc (the macOS default), and bash login
    shells on macOS read ~/.bash_profile. Everything else answers None rather than falling back
    to ~/.profile — fish and nushell never read it, and a POSIX `export` line is not their syntax
    either, so writing it would leave a file that does nothing and a command still not found.
    """
    name = os.path.basename(shell or "")
    if name == "zsh":
        return _home() / ".zshrc"
    if name == "bash":
        return _home() / ".bash_profile"
    return None


def shell_rc_candidates() -> list[Path]:
    """Every rc file any version of the installer might have put its PATH block in.

    Removal reads this rather than asking about the shell running right now: install under zsh and
    uninstall from bash and the block would otherwise be left behind for good. ~/.profile is here
    because installs before this ever wrote it, even though nothing writes it now.
    """
    home = _home()
    return [home / ".zshrc", home / ".bash_profile", home / ".profile"]


def tcc_protected_roots() -> list[Path]:
    """The user dirs macOS gates behind per-app TCC consent. A launchd job gets no consent
    prompt — it just fails to read — so an install tree must not live under any of these.

    Empty elsewhere: nothing else has this concept, and a check against a list that means nothing
    would refuse install locations for no reason.
    """
    if not host.macos():
        return []
    home = _home()
    return [home / "Documents", home / "Desktop", home / "Downloads"]


def launch_agents_dir(platform=None) -> Path:
    """Where job definitions live, composed here from the platform's OS-specific rule
    (callers must never compose this themselves). A platform may be injected (tests); otherwise
    the host platform is used. home is resolved here so the platform layer never touches it.

    A platform with no auto-start directory of the OS's own (Windows: registration is an API
    call, not a file placement) answers None, and the definitions stay in our config tree — one
    tree to install, inspect and remove."""
    resolved = platform if platform is not None else get_platform()
    system_dir = resolved.launch_agents_dir(_home())
    return system_dir if system_dir is not None else config_dir() / "tasks"


# --- ensure helpers ------------------------------------------------------------------------


def ensure_private_dir(path: Path) -> Path:
    """Create a directory only its owner can read, from creation — for one made outside startup,
    like a per-pass workspace."""
    return _ensure(path, 0o700)


def _ensure(path: Path, mode: int) -> Path:
    """Create a directory with an exact mode, from creation (umask neutralized so a sensitive
    mode like 0700 is never widened, and never applied via a post-creation chmod window).

    Windows ignores the mode, and there is no attempt to emulate it. Privacy there rests on the
    profile: %LOCALAPPDATA% is already unreadable by other non-administrator users, which is the
    same protection Claude Code relies on for its own credentials. Faking a chmod that enforced
    nothing would only make the code look like it had done something.
    """
    if host.windows():
        path.mkdir(parents=True, exist_ok=True)
        return path
    old_umask = os.umask(0)
    try:
        path.mkdir(mode=mode, parents=True, exist_ok=True)
    finally:
        os.umask(old_umask)
    return path


def ensure_data_dirs() -> None:
    _ensure(data_root(), 0o755)
    _ensure(versions_dir(), 0o755)
    _ensure(data_dir(), 0o755)
    _ensure(media_dir(), 0o755)
    # 0700: the profile holds the seller's live marketplace sessions (cookies), so it is as
    # sensitive as a credential file even though it is not one.
    _ensure(browser_profile_dir(), 0o700)


def ensure_state_dirs() -> None:
    _ensure(state_dir(), 0o755)
    _ensure(backups_dir(), 0o755)
    _ensure(logs_dir(), 0o755)
    # 0700: a browser publish stages the item's photographs into its pass workspace.
    _ensure(passes_dir(), 0o700)
    # 0700: page snapshots can carry the seller's own address off a composer page.
    _ensure(browser_output_dir(), 0o700)


def ensure_config_dir() -> None:
    # 0700 from creation: the config dir holds secrets in later workstreams.
    _ensure(config_dir(), 0o700)


def ensure_runtime_dirs() -> None:
    """Everything the daemon needs present before it starts."""
    ensure_data_dirs()
    ensure_state_dirs()
    ensure_config_dir()
