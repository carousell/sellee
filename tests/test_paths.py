"""paths.py resolves against XDG overrides and creates dirs with the right modes."""

from __future__ import annotations

import os

from selly_agent import paths


def test_xdg_overrides_are_honored(xdg_tmp) -> None:
    assert paths.data_root() == xdg_tmp / "data" / "selly-agent"
    assert paths.state_dir() == xdg_tmp / "state" / "selly-agent"
    assert paths.config_dir() == xdg_tmp / "config" / "selly-agent"
    assert paths.cache_dir() == xdg_tmp / "cache" / "selly-agent"


def test_derived_paths_hang_off_the_roots(xdg_tmp) -> None:
    assert paths.selly_db() == paths.data_dir() / "selly.db"
    assert paths.versions_dir() == paths.data_root() / "versions"
    assert paths.current() == paths.data_root() / "current"
    assert paths.events_db() == paths.state_dir() / "events.db"
    assert paths.backups_dir() == paths.state_dir() / "backups"
    assert paths.logs_dir() == paths.state_dir() / "logs"
    assert paths.heartbeat_path() == paths.state_dir() / "daemon.heartbeat.json"
    assert paths.lock_path() == paths.state_dir() / "daemon.lock"
    assert paths.config_path() == paths.config_dir() / "config.json"
    assert paths.browser_profile_dir() == paths.data_root() / "browser-profile"


def test_default_layout_without_xdg(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    assert paths.data_root() == home / ".local/share/selly-agent"
    assert paths.state_dir() == home / ".local/state/selly-agent"
    assert paths.config_dir() == home / ".config/selly-agent"
    assert paths.cache_dir() == home / ".cache/selly-agent"


def test_config_dir_is_0700_from_creation(xdg_tmp) -> None:
    paths.ensure_config_dir()
    mode = os.stat(paths.config_dir()).st_mode & 0o777
    assert mode == 0o700


def test_the_browser_profile_is_0700_from_creation(xdg_tmp) -> None:
    """The profile holds the seller's live marketplace sessions, so it is as sensitive as a
    credential file even though it is not one."""
    paths.ensure_data_dirs()
    assert os.stat(paths.browser_profile_dir()).st_mode & 0o777 == 0o700


def test_ensure_runtime_dirs_creates_everything(xdg_tmp) -> None:
    paths.ensure_runtime_dirs()
    for d in (
        paths.data_root(),
        paths.versions_dir(),
        paths.data_dir(),
        paths.browser_profile_dir(),
        paths.state_dir(),
        paths.backups_dir(),
        paths.logs_dir(),
        paths.config_dir(),
    ):
        assert d.is_dir()
