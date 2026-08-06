"""`selly-agent uninstall`: what it removes, what it refuses to touch, and what it preserves."""

from __future__ import annotations

import pytest
from tests.test_supervisor import FakePlatform

from selly_agent import cli, paths, supervisor, uninstall_cli
from selly_agent.installer import materialize


@pytest.fixture
def installed(xdg_tmp, tree, monkeypatch):
    """A complete install: a version, a shim, a launchd job, data, secrets and an rc block."""
    platform = FakePlatform()
    monkeypatch.setattr(uninstall_cli, "get_platform", lambda: platform)
    materialize.install_version(tree, "1.0.0")
    materialize.install_shim()
    supervisor.install(mode="login-start", platform=platform)

    paths.ensure_runtime_dirs()
    paths.selly_db().write_text("the seller's listings")
    paths.carousell_ai_api_key_path().write_text("rail-identity\n")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    materialize.add_rc_block(materialize.shell_rc_target("/bin/zsh"))
    return platform


def uninstall_main(*argv) -> int:
    return cli.main(["selly-agent", "uninstall", *argv])


def test_a_full_uninstall_leaves_nothing_of_ours_behind(installed) -> None:
    assert uninstall_main("--yes") == 0

    assert not paths.data_root().exists()
    assert not paths.state_dir().exists()
    assert not paths.config_dir().exists()
    assert not paths.cache_dir().exists()
    assert not paths.shim_path().exists()
    assert not installed.is_registered("com.selly.agent")
    assert not (paths.launch_agents_dir(platform=installed) / "com.selly.agent.plist").exists()
    assert not materialize.rc_block_present(materialize.shell_rc_target("/bin/zsh").read_text())


def test_a_container_uninstall_refuses_rather_than_emptying_the_bind_mount(
    container, installed, capsys
) -> None:
    """Run here it would empty the seller's bind-mounted directory and leave the container up.
    Everything this install wrote is in that one directory, which is theirs to delete."""
    assert uninstall_main("--yes") == 1

    assert paths.selly_db().exists()
    assert installed.is_registered("com.selly.agent")
    err = capsys.readouterr().err
    assert "/data" in err
    assert "docker" not in err.lower()


def test_after_uninstalling_a_fresh_install_is_not_blocked(installed) -> None:
    # The bug class this guards: leftovers that make the next ./setup refuse — a real directory
    # where `current` should be a symlink, or a shim that is already taken.
    uninstall_main("--yes")
    assert not paths.current().exists()
    assert not paths.shim_path().exists()


def test_preserve_data_keeps_the_data_and_the_key_it_depends_on(installed) -> None:
    assert uninstall_main("--yes", "--preserve-data") == 0

    assert paths.selly_db().read_text() == "the seller's listings"
    assert paths.browser_profile_dir().is_dir()
    # The key is the rail identity every one of those listings was created under: preserving the
    # database without it would leave listings nobody can act on again.
    assert paths.carousell_ai_api_key_path().read_text().strip() == "rail-identity"

    # The code and the prunable state still go.
    assert not paths.versions_dir().exists()
    assert not paths.state_dir().exists()
    assert not paths.current().exists()


def test_declining_the_confirmation_changes_nothing(installed, monkeypatch, capsys) -> None:
    # Not interactive and no --yes: the destructive default is no.
    assert uninstall_main() == 0
    assert paths.selly_db().exists()
    assert installed.is_registered("com.selly.agent")
    assert "Left everything alone." in capsys.readouterr().out


def test_it_says_what_it_will_remove_before_removing_it(installed, capsys) -> None:
    uninstall_main("--yes")
    out = capsys.readouterr().out
    assert str(paths.data_root()) in out
    assert str(paths.shim_path()) in out


def test_a_foreign_shim_is_left_alone(installed, tmp_path) -> None:
    paths.shim_path().unlink()
    someone_else = tmp_path / "their-tool"
    someone_else.write_text("")
    paths.shim_path().symlink_to(someone_else)

    uninstall_main("--yes")

    assert paths.shim_path().is_symlink()  # not ours, not touched


def test_a_foreign_plist_with_our_label_is_left_alone(installed) -> None:
    plist = paths.launch_agents_dir(platform=installed) / "com.selly.agent.plist"
    plist.write_text("<plist>someone else's daemon</plist>")

    uninstall_main("--yes")

    assert plist.read_text() == "<plist>someone else's daemon</plist>"
