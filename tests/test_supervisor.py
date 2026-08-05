"""launchd integration: golden plist render, and mode logic with launchctl stubbed."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from selly_agent import config, paths, supervisor
from selly_agent.installer import materialize
from selly_agent.platform.macos import MacOSPlatform

GOLDEN = Path(__file__).resolve().parent / "golden" / "com.selly.agent.plist"


class FakePlatform(MacOSPlatform):
    """A macOS platform whose launchctl calls are recorded in-memory instead of executed."""

    def __init__(self):
        self.registered_labels: set[str] = set()
        self.register_calls: list[Path] = []
        self.unregister_calls: list[str] = []

    def register(self, config_path: Path) -> None:
        self.register_calls.append(Path(config_path))
        self.registered_labels.add(Path(config_path).stem)

    def unregister(self, label: str) -> None:
        self.unregister_calls.append(label)
        self.registered_labels.discard(label)

    def is_registered(self, label: str) -> bool:
        return label in self.registered_labels


# --- pure render --------------------------------------------------------------------------


def test_plist_render_matches_golden() -> None:
    text = MacOSPlatform().render_supervisor(
        label="com.selly.agent",
        program_args=["/usr/bin/python3", "/opt/current/bin/selly-agent", "daemon", "run"],
        stdout_path=Path("/state/logs/agent.out.log"),
        stderr_path=Path("/state/logs/agent.err.log"),
        marker=supervisor.MARKER,
        environment={},
    )
    assert text == GOLDEN.read_text()


def test_the_plist_pins_the_xdg_overrides_the_installer_ran_under(xdg_tmp) -> None:
    # launchd hands the job its own environment, not the installing shell's. Without these
    # pinned, an install under XDG overrides provisions state in one world and boots a daemon
    # that resolves every root in another — the daemon comes up healthy and heartbeats where
    # nobody is looking.
    fake = FakePlatform()
    assert supervisor.install(mode="manual", platform=fake) == 0

    plist = (paths.config_dir() / "com.selly.agent.plist").read_text()
    assert "EnvironmentVariables" in plist
    for var in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        assert f"<key>{var}</key>" in plist
        assert f"<string>{os.environ[var]}</string>" in plist


def test_the_job_runs_on_the_installs_own_venv_interpreter(xdg_tmp, tree) -> None:
    # The daemon needs the dependencies, and a supervised job inherits no shell — so the
    # definition has to name the venv's interpreter rather than whatever ran the installer.
    materialize.install_version(tree, "1.0.0")
    interpreter = paths.venv_python(paths.current())

    assert supervisor.job_interpreter() == interpreter

    fake = FakePlatform()
    assert supervisor.install(mode="manual", platform=fake) == 0
    plist = (paths.config_dir() / "com.selly.agent.plist").read_text()
    assert f"<string>{interpreter}</string>" in plist


def test_the_job_names_the_interpreter_through_current_not_a_version(xdg_tmp, tree) -> None:
    # Named through the swap point, so the definition does not go stale the moment a version is
    # replaced underneath it — and so an update's re-register writes the same path back.
    materialize.install_version(tree, "1.0.0")
    named = str(supervisor.job_interpreter())
    assert named.startswith(str(paths.current()))
    assert "versions" not in named


def test_a_checkout_without_a_venv_still_gets_a_startable_job(xdg_tmp) -> None:
    # `./setup --dev` before a bootstrap: naming an interpreter that does not exist would give a
    # job that fails to start with nothing saying why.
    assert supervisor.job_interpreter() == Path(os.path.realpath(sys.executable))


def test_the_plist_puts_the_recorded_node_directory_on_the_jobs_path(xdg_tmp) -> None:
    # A supervised job's PATH holds none of a version manager's directories, so `npx` — and so the
    # whole browser layer — is unreachable unless the installer's answer is carried here.
    config.merge_into_file({"node_bin_dir": "/opt/node-versions/v22/bin"})
    fake = FakePlatform()
    assert supervisor.install(mode="manual", platform=fake) == 0

    plist = (paths.config_dir() / "com.selly.agent.plist").read_text()
    assert "<key>PATH</key>" in plist
    assert f"<string>/opt/node-versions/v22/bin:{supervisor.SUPERVISED_PATH}</string>" in plist


def test_a_multi_directory_fragment_reaches_the_jobs_path_whole(xdg_tmp) -> None:
    # On a machine where `node` and `npx` live apart, the recorded value is already a PATH
    # fragment; the job needs every entry, not the first one.
    config.merge_into_file({"node_bin_dir": "/opt/node/bin:/usr/local/npm-global/bin"})
    assert supervisor.install(mode="manual", platform=FakePlatform()) == 0

    plist = (paths.config_dir() / "com.selly.agent.plist").read_text()
    assert (
        f"<string>/opt/node/bin:/usr/local/npm-global/bin:{supervisor.SUPERVISED_PATH}</string>"
        in plist
    )


def test_no_recorded_node_directory_leaves_the_jobs_path_alone(xdg_tmp) -> None:
    # Nothing recorded means nothing known: naming a PATH here would replace the default with a
    # guess, and a daemon started from a shell already has the right one.
    fake = FakePlatform()
    assert supervisor.install(mode="manual", platform=fake) == 0

    plist = (paths.config_dir() / "com.selly.agent.plist").read_text()
    assert "<key>PATH</key>" not in plist


# --- mode logic ---------------------------------------------------------------------------


def test_install_manual_places_in_config_dir_and_does_not_register(xdg_tmp) -> None:
    fake = FakePlatform()
    rc = supervisor.install(mode="manual", platform=fake)
    assert rc == 0

    plist = paths.config_dir() / "com.selly.agent.plist"
    assert plist.exists() and supervisor.MARKER in plist.read_text()
    assert fake.register_calls == []  # manual mode does not auto-start
    assert config.load().daemon_mode == "manual"
    assert paths.current().is_symlink()


def test_install_login_start_places_in_launch_agents_and_registers(xdg_tmp) -> None:
    fake = FakePlatform()
    rc = supervisor.install(mode="login-start", platform=fake)
    assert rc == 0

    plist = paths.launch_agents_dir(platform=fake) / "com.selly.agent.plist"
    assert plist.exists()
    assert fake.is_registered("com.selly.agent")
    assert config.load().daemon_mode == "login-start"


def test_flip_moves_the_plist(xdg_tmp) -> None:
    fake = FakePlatform()
    supervisor.install(mode="manual", platform=fake)
    manual_plist = paths.config_dir() / "com.selly.agent.plist"
    assert manual_plist.exists()

    supervisor.install(mode="login-start", platform=fake)
    login_plist = paths.launch_agents_dir(platform=fake) / "com.selly.agent.plist"
    assert login_plist.exists()
    assert not manual_plist.exists()  # moved, not duplicated
    assert config.load().daemon_mode == "login-start"


def test_install_refuses_foreign_plist(xdg_tmp) -> None:
    fake = FakePlatform()
    la_dir = paths.launch_agents_dir(platform=fake)
    la_dir.mkdir(parents=True)
    foreign = la_dir / "com.selly.agent.plist"
    foreign.write_text("<plist>legacy daemon, not ours</plist>")

    rc = supervisor.install(mode="login-start", platform=fake)
    assert rc == 2
    assert foreign.read_text() == "<plist>legacy daemon, not ours</plist>"  # untouched
    assert fake.register_calls == []


def test_start_then_stop(xdg_tmp) -> None:
    fake = FakePlatform()
    supervisor.install(mode="manual", platform=fake)

    assert supervisor.start(platform=fake) == 0
    assert fake.is_registered("com.selly.agent")
    assert supervisor.start(platform=fake) == 0  # idempotent friendly no-op

    assert supervisor.stop(platform=fake) == 0
    assert not fake.is_registered("com.selly.agent")


def test_start_without_install_reports_not_installed(xdg_tmp) -> None:
    fake = FakePlatform()
    assert supervisor.start(platform=fake) == 2


def test_uninstall_removes_our_plist(xdg_tmp) -> None:
    fake = FakePlatform()
    supervisor.install(mode="login-start", platform=fake)
    assert supervisor.uninstall(platform=fake) == 0
    assert not (paths.launch_agents_dir(platform=fake) / "com.selly.agent.plist").exists()
    assert not fake.is_registered("com.selly.agent")


def test_status_manual_stopped(xdg_tmp) -> None:
    fake = FakePlatform()
    supervisor.install(mode="manual", platform=fake)
    st = supervisor.gather_status(platform=fake)
    assert st.mode == "manual"
    assert st.registered is False
    assert st.label == "com.selly.agent"


def test_label_override_is_recorded_and_used(xdg_tmp) -> None:
    fake = FakePlatform()
    supervisor.install(mode="login-start", label="com.selly.agent.dev", platform=fake)
    assert (paths.launch_agents_dir(platform=fake) / "com.selly.agent.dev.plist").exists()
    assert config.load().daemon_label == "com.selly.agent.dev"
    assert supervisor.gather_status(platform=fake).label == "com.selly.agent.dev"


# --- the layout `daemon install` provisions -------------------------------------------------


def test_install_leaves_an_existing_versioned_current_alone(xdg_tmp) -> None:
    # `update` swaps current to the new version and then re-runs `daemon install` to re-render
    # the plist. If install re-pointed current at the tree its own code lives in, the update
    # would silently undo its own swap.
    fake = FakePlatform()
    version = paths.versions_dir() / "9.9.9"
    (version / "bin").mkdir(parents=True)
    paths.ensure_runtime_dirs()
    paths.current().symlink_to(version)

    assert supervisor.install(mode="manual", platform=fake) == 0
    assert Path(os.path.realpath(paths.current())) == version.resolve()


def test_install_points_current_at_the_checkout_when_nothing_is_installed(xdg_tmp) -> None:
    fake = FakePlatform()
    assert supervisor.install(mode="manual", platform=fake) == 0
    assert paths.current().is_symlink()
    assert (paths.current() / "bin" / "selly-agent").is_file()


def test_install_refuses_a_real_directory_at_current(xdg_tmp) -> None:
    fake = FakePlatform()
    paths.ensure_runtime_dirs()
    paths.current().mkdir(parents=True)

    assert supervisor.install(mode="manual", platform=fake) == 2
    assert fake.register_calls == []


# --- the confirmed stop -------------------------------------------------------------------


def _liveness(*answers):
    """Successive answers to is_pid_alive, so a test can script a process going away. The last
    answer repeats, so a single True means one that never does."""
    remaining = list(answers)
    return lambda _pid: remaining.pop(0) if len(remaining) > 1 else remaining[0]


def _pretend_holder(monkeypatch, pid: int, *answers) -> None:
    monkeypatch.setattr(supervisor.lock, "read_holder_pid", lambda _path: pid)
    monkeypatch.setattr(supervisor.lock, "is_pid_alive", _liveness(*answers))
    monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: None)


def test_stopping_deregisters_before_asking_the_daemon_to_drain(xdg_tmp, monkeypatch) -> None:
    """Order matters where a periodic trigger is the keep-alive: a daemon asked to stop while its
    job is still enabled can be started again before it has finished going."""
    fake = FakePlatform()
    fake.registered_labels.add("com.selly.agent")
    happened = []

    def unregister(label: str) -> None:
        happened.append("deregistered")
        fake.registered_labels.discard(label)

    def post(*_args, **_kwargs):
        happened.append("asked")
        return 202, {"stopping": True}

    monkeypatch.setattr(fake, "unregister", unregister)
    monkeypatch.setattr(supervisor.secrets, "read_mcp_token", lambda: "token")
    monkeypatch.setattr(supervisor.control, "post", post)
    _pretend_holder(monkeypatch, 4242, True, False)

    assert supervisor.shutdown(platform=fake) is True
    assert happened == ["deregistered", "asked"]


def test_a_daemon_that_will_not_go_is_not_reported_as_stopped(xdg_tmp, monkeypatch) -> None:
    """Nothing is forced: whatever it is still doing, it is doing to the seller's databases."""
    fake = FakePlatform()
    monkeypatch.setattr(supervisor.secrets, "read_mcp_token", lambda: None)
    monkeypatch.setattr(supervisor, "STOP_TIMEOUT_SEC", 0.0)
    _pretend_holder(monkeypatch, 4242, True)

    assert supervisor.shutdown(platform=fake) is False
    assert supervisor.stop(platform=fake) == 1


def test_an_unreachable_daemon_still_settles_when_its_process_goes(xdg_tmp, monkeypatch) -> None:
    """A daemon already draining refuses connections while still holding the lock, which is no
    reason to give up on it: the process going away is the answer, not the reply."""
    fake = FakePlatform()

    def refuse(*_args, **_kwargs):
        raise supervisor.control.DaemonUnreachable("connection refused")

    monkeypatch.setattr(supervisor.secrets, "read_mcp_token", lambda: "token")
    monkeypatch.setattr(supervisor.control, "post", refuse)
    _pretend_holder(monkeypatch, 4242, True, True, False)

    assert supervisor.shutdown(platform=fake) is True
