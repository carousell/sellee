"""systemd integration: the golden unit render, and the mode logic with systemctl stubbed.

The mode logic itself is portable and covered in test_supervisor.py against the macOS platform.
What is re-run here is only what the mapping onto systemctl can get wrong — which verb each mode
reaches for, and whether a flip and an uninstall leave the unit directory clean.
"""

from __future__ import annotations

import os
from pathlib import Path

from selly_agent import config, paths, supervisor
from selly_agent.platform.linux import LinuxPlatform

GOLDEN = Path(__file__).resolve().parent / "golden" / "selly-agent.service"

UNIT = "selly-agent.service"


class FakeLinuxPlatform(LinuxPlatform):
    """A Linux platform whose systemctl calls are recorded in-memory instead of executed.

    Recorded at the argv level rather than at register()/unregister(), because the argv *is* the
    behaviour under test: `enable` and `link` differ in whether the unit comes back at next login.
    """

    def __init__(self):
        self.calls: list[list[str]] = []
        self.active: set[str] = set()

    def _systemctl(self, *args: str):
        self.calls.append(list(args))
        verb = args[0]
        if verb in ("enable", "start"):
            self.active.add(Path(args[-1]).name)
        elif verb == "disable":
            self.active.discard(args[-1])
        elif verb == "is-active":
            return _Completed(0 if args[-1] in self.active else 3)
        return _Completed(0)

    def verbs(self) -> list[str]:
        return [call[0] for call in self.calls]


class _Completed:
    def __init__(self, returncode: int):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


# --- pure render --------------------------------------------------------------------------


def test_unit_render_matches_golden() -> None:
    text = LinuxPlatform().render_supervisor(
        label="selly-agent",
        program_args=[
            "/opt/current/.venv/bin/python",
            "/opt/current/bin/selly-agent",
            "daemon",
            "run",
        ],
        stdout_path=Path("/state/logs/agent.out.log"),
        stderr_path=Path("/state/logs/agent.err.log"),
        marker=supervisor.MARKER,
        environment={},
    )
    assert text == GOLDEN.read_text()


def test_the_unit_carries_the_marker_that_makes_it_ours() -> None:
    text = _render()
    assert text.splitlines()[0] == f"# {supervisor.MARKER}"


def test_a_crash_loop_is_paced_rather_than_parked() -> None:
    """systemd's default start limiter would leave a crash-looping unit in `failed` until
    somebody ran `reset-failed` by hand, which launchd's throttle never does. Disabling the
    limiter and pacing with RestartSec keeps a machine that is briefly broken self-healing."""
    text = _render()
    assert "StartLimitIntervalSec=0" in text
    assert "Restart=on-failure" in text
    assert "RestartSec=30" in text
    # In [Unit]: systemd moved these out of [Service], where a modern one ignores them.
    unit_section = text.split("[Service]")[0]
    assert "StartLimitIntervalSec=0" in unit_section


def test_a_clean_exit_is_not_respawned() -> None:
    """A duplicate start exits 0 through the instance lock, and `daemon stop` exits 0 too —
    neither should come straight back."""
    assert "Restart=always" not in _render()
    assert "Restart=on-failure" in _render()


def test_the_unit_starts_at_login_only_where_it_is_installed_to() -> None:
    """Placement decides the mode, so the unit body itself must be identical either way — it is
    the [Install] section systemd acts on, and only when the unit is enabled."""
    assert "WantedBy=default.target" in _render()


def test_argv_survives_a_home_directory_with_a_space_in_it() -> None:
    """systemd splits ExecStart on whitespace, so an unquoted path under /home/Ada Lovelace
    becomes two arguments and the job fails to start with a file-not-found nobody expects."""
    text = _render(program_args=["/home/Ada Lovelace/py", "/home/Ada Lovelace/bin/selly-agent"])
    assert 'ExecStart="/home/Ada Lovelace/py" "/home/Ada Lovelace/bin/selly-agent"' in text


def test_a_percent_in_a_path_is_not_read_as_a_systemd_specifier() -> None:
    """`%h` and friends expand inside a unit file, so a literal percent has to be doubled or the
    daemon is pointed somewhere nobody chose."""
    text = _render(environment={"XDG_DATA_HOME": "/tmp/100%sure/share"})
    assert 'Environment="XDG_DATA_HOME=/tmp/100%%sure/share"' in text


def test_the_log_paths_are_appended_to_rather_than_truncated() -> None:
    """`retention.py` trims these files to a size cap and the log commands read them in place, so
    they have to be the same files across a restart."""
    text = _render(
        stdout_path=Path("/state/logs/agent.out.log"),
        stderr_path=Path("/state/logs/agent.err.log"),
    )
    assert "StandardOutput=append:/state/logs/agent.out.log" in text
    assert "StandardError=append:/state/logs/agent.err.log" in text


def _render(**overrides) -> str:
    kwargs = {
        "label": "selly-agent",
        "program_args": ["/usr/bin/python3", "/opt/current/bin/selly-agent", "daemon", "run"],
        "stdout_path": Path("/state/logs/agent.out.log"),
        "stderr_path": Path("/state/logs/agent.err.log"),
        "marker": supervisor.MARKER,
        "environment": {},
    }
    kwargs.update(overrides)
    return LinuxPlatform().render_supervisor(**kwargs)


# --- what the installer pins into the unit ---------------------------------------------------


def test_the_unit_pins_the_xdg_overrides_the_installer_ran_under(xdg_tmp) -> None:
    fake = FakeLinuxPlatform()
    assert supervisor.install(mode="manual", platform=fake) == 0

    unit = (paths.config_dir() / UNIT).read_text()
    for var in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        assert f'Environment="{var}={os.environ[var]}"' in unit


def test_the_unit_puts_the_recorded_node_directory_on_the_jobs_path(xdg_tmp) -> None:
    config.merge_into_file({"node_bin_dir": "/opt/node-versions/v22/bin"})
    assert supervisor.install(mode="manual", platform=FakeLinuxPlatform()) == 0

    unit = (paths.config_dir() / UNIT).read_text()
    assert f'Environment="PATH=/opt/node-versions/v22/bin:{supervisor.SUPERVISED_PATH}"' in unit


# --- the systemctl mapping ---------------------------------------------------------------------


def test_login_start_enables_the_unit_from_the_user_unit_directory(xdg_tmp) -> None:
    fake = FakeLinuxPlatform()
    assert supervisor.install(mode="login-start", platform=fake) == 0

    assert (paths.launch_agents_dir(platform=fake) / UNIT).exists()
    assert fake.verbs() == ["daemon-reload", "enable"]
    assert fake.calls[-1] == ["enable", "--now", UNIT]
    assert fake.is_registered("selly-agent")


def test_manual_mode_links_the_unit_without_enabling_it(xdg_tmp) -> None:
    """Linked but not enabled is exactly what a plist kept out of the launch-agents dir means:
    startable by name, and never brought back by a login."""
    fake = FakeLinuxPlatform()
    assert supervisor.install(mode="manual", platform=fake) == 0
    assert fake.calls == []  # manual mode does not auto-start

    assert supervisor.start(platform=fake) == 0
    # The leading is-active is start's own "already running?" question.
    assert fake.verbs() == ["is-active", "daemon-reload", "link", "start"]
    assert fake.calls[2] == ["link", "--force", str(paths.config_dir() / UNIT)]
    assert "enable" not in fake.verbs()


def test_stop_disables_the_unit(xdg_tmp) -> None:
    fake = FakeLinuxPlatform()
    supervisor.install(mode="login-start", platform=fake)

    assert supervisor.stop(platform=fake) == 0
    assert fake.calls[-1] == ["disable", "--now", UNIT]
    assert not fake.is_registered("selly-agent")


def test_a_flip_moves_the_unit_and_disables_it_at_the_old_location(xdg_tmp) -> None:
    """`disable` takes back both the wants-symlink and the link-symlink, so nothing is left
    pointing at a file that is no longer there."""
    fake = FakeLinuxPlatform()
    supervisor.install(mode="login-start", platform=fake)
    login_unit = paths.launch_agents_dir(platform=fake) / UNIT

    supervisor.install(mode="manual", platform=fake)

    assert not login_unit.exists()
    assert (paths.config_dir() / UNIT).exists()
    assert ["disable", "--now", UNIT] in fake.calls


def test_uninstall_disables_the_unit_and_removes_the_file(xdg_tmp) -> None:
    fake = FakeLinuxPlatform()
    supervisor.install(mode="login-start", platform=fake)

    assert supervisor.uninstall(platform=fake) == 0
    assert not (paths.launch_agents_dir(platform=fake) / UNIT).exists()
    assert ["disable", "--now", UNIT] in fake.calls


def test_the_unit_file_is_reloaded_before_it_is_acted_on(xdg_tmp) -> None:
    """`daemon-reload` first, or systemd acts on the definition it cached before this install
    rewrote the file — which after an update is the previous version's interpreter."""
    fake = FakeLinuxPlatform()
    supervisor.install(mode="login-start", platform=fake)
    assert fake.verbs()[0] == "daemon-reload"


def test_install_refuses_a_foreign_unit_with_our_name(xdg_tmp) -> None:
    fake = FakeLinuxPlatform()
    unit_dir = paths.launch_agents_dir(platform=fake)
    unit_dir.mkdir(parents=True)
    foreign = unit_dir / UNIT
    foreign.write_text("[Service]\nExecStart=/bin/true\n")

    assert supervisor.install(mode="login-start", platform=fake) == 2
    assert foreign.read_text() == "[Service]\nExecStart=/bin/true\n"
    assert fake.calls == []


def test_the_default_label_cannot_collide_with_the_macos_one(xdg_tmp) -> None:
    """The label is recorded per machine, so the two defaults never meet — but a Linux unit named
    `com.selly.agent.service` would still be legal, and this is what stops it happening by
    accident."""
    fake = FakeLinuxPlatform()
    supervisor.install(mode="manual", platform=fake)
    assert config.load().daemon_label == "selly-agent"
    assert (paths.config_dir() / "selly-agent.service").exists()


# --- the unit directory --------------------------------------------------------------------------


def test_the_unit_directory_is_composed_from_the_home_it_is_given(tmp_path) -> None:
    """Composed from the argument rather than read from the environment: home resolution belongs
    to paths.py, and a platform that reached for it would resolve a different one under test."""
    assert LinuxPlatform().launch_agents_dir(tmp_path) == tmp_path / ".config/systemd/user"
