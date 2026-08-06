"""paths.py resolves against the overrides and the OS convention, and creates private dirs."""

from __future__ import annotations

import os

import pytest

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
    assert paths.browser_output_dir() == paths.state_dir() / "browser-output"


def _no_overrides(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for var in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.mark.skipif(os.name == "nt", reason="the XDG layout")
def test_default_layout_without_xdg(monkeypatch, tmp_path) -> None:
    home = _no_overrides(monkeypatch, tmp_path)
    assert paths.data_root() == home / ".local/share/selly-agent"
    assert paths.state_dir() == home / ".local/state/selly-agent"
    assert paths.config_dir() == home / ".config/selly-agent"
    assert paths.cache_dir() == home / ".cache/selly-agent"


@pytest.mark.skipif(os.name != "nt", reason="the Windows layout")
def test_default_layout_on_windows_is_one_local_tree(monkeypatch, tmp_path) -> None:
    """Four subtrees of one directory rather than four locations, and never the roaming profile:
    the browser profile, the WAL databases and the secrets must not be copied between machines."""
    _no_overrides(monkeypatch, tmp_path)
    local = tmp_path / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    root = local / "selly-agent"

    assert paths.data_root() == root / "share"
    assert paths.state_dir() == root / "state"
    assert paths.config_dir() == root / "config"
    assert paths.cache_dir() == root / "cache"


def test_an_override_wins_on_every_platform(monkeypatch, tmp_path) -> None:
    """The overrides are a power user's escape hatch and the suite's own seam, so they cannot be
    a POSIX-only feature."""
    _no_overrides(monkeypatch, tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "elsewhere"))
    assert paths.data_root() == tmp_path / "elsewhere" / "selly-agent"


@pytest.mark.skipif(os.name == "nt", reason="no file mode to assert; privacy is the profile")
def test_config_dir_is_0700_from_creation(xdg_tmp) -> None:
    paths.ensure_config_dir()
    mode = os.stat(paths.config_dir()).st_mode & 0o777
    assert mode == 0o700


@pytest.mark.skipif(os.name == "nt", reason="no file mode to assert; privacy is the profile")
def test_the_browser_dirs_are_0700_from_creation(xdg_tmp) -> None:
    paths.ensure_data_dirs()
    paths.ensure_state_dirs()
    assert os.stat(paths.browser_profile_dir()).st_mode & 0o777 == 0o700
    assert os.stat(paths.browser_output_dir()).st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name == "nt", reason="no file mode to assert; privacy is the profile")
def test_the_pass_workspaces_are_0700_from_creation(xdg_tmp) -> None:
    paths.ensure_state_dirs()
    assert os.stat(paths.passes_dir()).st_mode & 0o777 == 0o700


def test_ensure_runtime_dirs_creates_everything(xdg_tmp) -> None:
    paths.ensure_runtime_dirs()
    for d in (
        paths.data_root(),
        paths.versions_dir(),
        paths.data_dir(),
        paths.browser_profile_dir(),
        paths.browser_output_dir(),
        paths.state_dir(),
        paths.backups_dir(),
        paths.logs_dir(),
        paths.config_dir(),
    ):
        assert d.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="no file mode to assert; privacy is the profile")
def test_a_pass_workspace_is_private_from_creation(xdg_tmp) -> None:
    """It is created mid-run, not at startup, and a browser publish stages photographs into it."""
    workspace = paths.pass_workspace_dir("pass_x")
    paths.ensure_private_dir(workspace)
    assert os.stat(workspace).st_mode & 0o777 == 0o700
