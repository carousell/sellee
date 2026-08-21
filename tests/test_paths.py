"""paths.py resolves against XDG overrides and creates dirs with the right modes."""

from __future__ import annotations

import os
import sys

import pytest

from sellee import deployment, paths

# Windows does not honor POSIX modes, so every mode assertion is meaningless there.
posix_only = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX file modes")


def test_xdg_overrides_are_honored(xdg_tmp) -> None:
    assert paths.data_root() == xdg_tmp / "data" / "sellee"
    assert paths.state_dir() == xdg_tmp / "state" / "sellee"
    assert paths.config_dir() == xdg_tmp / "config" / "sellee"
    assert paths.cache_dir() == xdg_tmp / "cache" / "sellee"


def test_derived_paths_hang_off_the_roots(xdg_tmp) -> None:
    assert paths.sellee_db() == paths.data_dir() / "sellee.db"
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


def test_default_layout_without_xdg(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    assert paths.data_root() == home / ".local/share/sellee"
    assert paths.state_dir() == home / ".local/state/sellee"
    assert paths.config_dir() == home / ".config/sellee"
    assert paths.cache_dir() == home / ".cache/sellee"


def test_config_dir_is_0700_from_creation(xdg_tmp) -> None:
    paths.ensure_config_dir()
    mode = os.stat(paths.config_dir()).st_mode & 0o777
    assert mode == 0o700


def test_the_browser_dirs_are_0700_from_creation(xdg_tmp) -> None:
    paths.ensure_data_dirs()
    paths.ensure_state_dirs()
    assert os.stat(paths.browser_profile_dir()).st_mode & 0o777 == 0o700
    assert os.stat(paths.browser_output_dir()).st_mode & 0o777 == 0o700


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


def test_a_pass_workspace_is_private_from_creation(xdg_tmp) -> None:
    """It is created mid-run, not at startup, and a browser publish stages photographs into it."""
    workspace = paths.pass_workspace_dir("pass_x")
    paths.ensure_private_dir(workspace)
    assert os.stat(workspace).st_mode & 0o777 == 0o700


# --- modes: intermediates, the state tree, files ------------------------------------------------


def _fresh_default_home(monkeypatch, tmp_path):
    """A HOME where ~/.local does not pre-exist — the exposed case for parent creation —
    with the XDG overrides cleared so the default layout resolves."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in paths._XDG_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv(deployment.MARKER_VAR, raising=False)
    return home


@posix_only
def test_missing_intermediates_are_created_sane_not_world_writable(monkeypatch, tmp_path) -> None:
    """mkdir(parents=True) under the zeroed umask used to leave ~/.local itself at 0o777."""
    home = _fresh_default_home(monkeypatch, tmp_path)
    paths.ensure_runtime_dirs()
    for intermediate in (
        home / ".local",
        home / ".local/share",
        home / ".local/state",
        home / ".config",
    ):
        assert os.stat(intermediate).st_mode & 0o777 == 0o755, intermediate


@posix_only
def test_the_state_tree_is_owner_only(xdg_tmp) -> None:
    paths.ensure_state_dirs()
    for d in (paths.state_dir(), paths.backups_dir(), paths.logs_dir()):
        assert os.stat(d).st_mode & 0o777 == 0o700, d


@posix_only
def test_a_private_file_is_owner_only_from_creation(xdg_tmp) -> None:
    paths.ensure_config_dir()
    target = paths.config_dir() / "some.db"
    paths.ensure_private_file(target)
    assert os.stat(target).st_mode & 0o777 == 0o600


@posix_only
def test_a_private_file_that_already_exists_wider_is_narrowed(xdg_tmp) -> None:
    paths.ensure_config_dir()
    target = paths.config_dir() / "some.db"
    target.write_bytes(b"")
    os.chmod(target, 0o644)
    paths.ensure_private_file(target)
    assert os.stat(target).st_mode & 0o777 == 0o600


@posix_only
def test_a_database_file_is_owner_only_from_creation(xdg_tmp) -> None:
    """The DB (not just its directory) must be 0600: sellee.db sits under the 0755 data dir."""
    from sellee.db import Database

    paths.ensure_runtime_dirs()
    db = Database(paths.sellee_db())
    try:
        db.run_atomic_script("BEGIN; CREATE TABLE t (x); COMMIT;")
        created = [paths.sellee_db(), *paths.sellee_db().parent.glob("sellee.db-*")]
        assert len(created) > 1  # WAL mode: the sidecars exist and carry the same content
        for file in created:
            assert os.stat(file).st_mode & 0o777 == 0o600, file
    finally:
        db.close()


@posix_only
def test_container_deployment_keeps_the_bind_mount_readable(xdg_tmp, monkeypatch) -> None:
    """In a container the daemon runs as root against the seller's bind-mounted data dir;
    owner-only modes there would lock the seller out of their own files on the host."""
    monkeypatch.setenv(deployment.MARKER_VAR, deployment.CONTAINER)
    old_umask = os.umask(0o022)
    try:
        paths.ensure_runtime_dirs()
        for d in (paths.state_dir(), paths.backups_dir(), paths.logs_dir()):
            assert os.stat(d).st_mode & 0o777 == 0o755, d
        target = paths.data_dir() / "sellee.db"
        paths.ensure_private_file(target)
        assert os.stat(target).st_mode & 0o777 == 0o644
    finally:
        os.umask(old_umask)


@posix_only
def test_container_intermediates_are_still_not_world_writable(monkeypatch, tmp_path) -> None:
    """The intermediates fix applies everywhere — 0o755 never blocks the owner, and a
    world-writable parent would let any host user write into the seller's data dir."""
    home = _fresh_default_home(monkeypatch, tmp_path)
    monkeypatch.setenv(deployment.MARKER_VAR, deployment.CONTAINER)
    paths.ensure_runtime_dirs()
    assert os.stat(home / ".local").st_mode & 0o777 == 0o755


# --- the shell rc a PATH export belongs in ------------------------------------------------------


def test_zsh_answers_zshrc_on_every_platform(monkeypatch) -> None:
    for platform in ("darwin", "linux"):
        monkeypatch.setattr(paths.sys, "platform", platform)
        assert paths.shell_rc_path("/bin/zsh").name == ".zshrc"


def test_bash_answers_the_file_that_platforms_shell_actually_reads(monkeypatch) -> None:
    """macOS Terminal opens login shells, which read ~/.bash_profile; a Linux desktop opens
    interactive ones, which read ~/.bashrc."""
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    assert paths.shell_rc_path("/bin/bash").name == ".bash_profile"

    monkeypatch.setattr(paths.sys, "platform", "linux")
    assert paths.shell_rc_path("/usr/bin/bash").name == ".bashrc"


def test_a_shell_we_cannot_be_right_about_is_left_alone(monkeypatch) -> None:
    for shell in ("/usr/bin/fish", "/usr/bin/nu", ""):
        assert paths.shell_rc_path(shell) is None


def test_removal_looks_in_every_file_any_install_could_have_written(monkeypatch) -> None:
    """Uninstall does not ask which shell is running now: install under zsh on a Mac, uninstall
    from bash on Linux, and the block would otherwise be left behind for good."""
    names = {path.name for path in paths.shell_rc_candidates()}
    assert {".zshrc", ".bash_profile", ".bashrc", ".profile"} <= names

    for platform in ("darwin", "linux"):
        monkeypatch.setattr(paths.sys, "platform", platform)
        for shell in ("/bin/zsh", "/bin/bash"):
            assert paths.shell_rc_path(shell) in paths.shell_rc_candidates()
