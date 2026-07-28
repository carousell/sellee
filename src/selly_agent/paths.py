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

from selly_agent.platform import get_platform

APP = "selly-agent"


def _home() -> Path:
    return Path.home()


def _xdg_base(var: str, default_rel: str) -> Path:
    override = os.environ.get(var)
    base = Path(override) if override else _home() / default_rel
    return base / APP


# --- XDG roots -----------------------------------------------------------------------------


def data_root() -> Path:
    """Immutable installs + business data live here (never pruned)."""
    return _xdg_base("XDG_DATA_HOME", ".local/share")


def state_dir() -> Path:
    """Transcripts, DB backups, logs — prunable by definition; safe to delete wholesale."""
    return _xdg_base("XDG_STATE_HOME", ".local/state")


def config_dir() -> Path:
    """config.json + secrets (0700)."""
    return _xdg_base("XDG_CONFIG_HOME", ".config")


def cache_dir() -> Path:
    """Downloaded release tarballs (regenerable)."""
    return _xdg_base("XDG_CACHE_HOME", ".cache")


# --- data_root children --------------------------------------------------------------------


def versions_dir() -> Path:
    return data_root() / "versions"


def current() -> Path:
    """The atomic swap point: a symlink into versions/<v> (dev mode: into the checkout)."""
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
    """Ephemeral per-pass workspaces (generated harness config only; swept on pass end)."""
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
    return [
        home / ".local" / "bin" / "claude",
        home / ".claude" / "local" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/opt/homebrew/bin/claude"),
    ]


def launch_agents_dir(platform=None) -> Path:
    """The per-user auto-start directory, composed here from the platform's OS-specific rule
    (callers must never compose this themselves). A platform may be injected (tests); otherwise
    the host platform is used. home is resolved here so the platform layer never touches it."""
    resolved = platform if platform is not None else get_platform()
    return resolved.launch_agents_dir(_home())


# --- ensure helpers ------------------------------------------------------------------------


def _ensure(path: Path, mode: int) -> Path:
    """Create a directory with an exact mode, from creation (umask neutralized so a sensitive
    mode like 0700 is never widened, and never applied via a post-creation chmod window)."""
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
    _ensure(passes_dir(), 0o755)
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
