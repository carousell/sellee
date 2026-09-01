"""launchd integration: golden plist render, and mode logic with launchctl stubbed."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from sellee import config, paths, supervisor
from sellee.installer import materialize
from sellee.platform.macos import MacOSPlatform

GOLDEN = Path(__file__).resolve().parent / "golden" / "com.sellee.agent.plist"


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


# --- letting go of a job ---------------------------------------------------------------------


def _late_release_launchctl(state, releases_after):
    """A launchctl whose `bootout` returns before `print` stops answering — the real one."""
    import subprocess as sp

    def fake(self, *args):
        if args[0] == "bootout":
            state["booted_out"] = True
        elif args[0] == "print":
            state["prints"] += 1
            gone = state["booted_out"] and state["prints"] > releases_after
            return sp.CompletedProcess(args, 1 if gone else 0, "", "")
        return sp.CompletedProcess(args, 0, "", "")

    return fake


def test_unregister_waits_for_launchctl_to_actually_let_go(monkeypatch) -> None:
    """`bootout` is asynchronous, and nothing noticed until a `daemon stop` was followed straight
    away by a `daemon start`: start asks `is_registered`, the half-torn-down job still answered
    yes, so start printed "already running" and did nothing — and then the bootout finished. No
    job, no process, and a heartbeat file fresh enough for `status` to call it running."""
    from sellee.platform import macos as macos_mod

    state = {"booted_out": False, "prints": 0}
    monkeypatch.setattr(MacOSPlatform, "_launchctl", _late_release_launchctl(state, 3))
    monkeypatch.setattr(macos_mod, "_BOOTOUT_POLL_SEC", 0.0)
    platform = MacOSPlatform()

    platform.unregister("com.sellee.agent")

    # The contract this method states: it stays stopped until re-registered. A caller asking
    # straight afterwards must get the truth.
    assert not platform.is_registered("com.sellee.agent")


def test_unregister_gives_up_rather_than_hanging(monkeypatch) -> None:
    """A launchctl that never lets go must not wedge the command with no output. The next command
    failing visibly is the better failure of the two."""
    from sellee.platform import macos as macos_mod

    state = {"booted_out": False, "prints": 0}
    # Never releases, however many times it is asked.
    monkeypatch.setattr(MacOSPlatform, "_launchctl", _late_release_launchctl(state, 10**9))
    monkeypatch.setattr(macos_mod, "_BOOTOUT_POLL_SEC", 0.0)
    monkeypatch.setattr(macos_mod, "_BOOTOUT_TIMEOUT_SEC", 0.05)
    platform = MacOSPlatform()

    platform.unregister("com.sellee.agent")  # returns rather than looping forever

    assert platform.is_registered("com.sellee.agent")


# --- pure render --------------------------------------------------------------------------


def test_plist_render_matches_golden() -> None:
    text = MacOSPlatform().render_supervisor(
        label="com.sellee.agent",
        program_args=["/usr/bin/python3", "/opt/current/bin/sellee", "daemon", "run"],
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

    plist = (paths.config_dir() / "com.sellee.agent.plist").read_text()
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
    plist = (paths.config_dir() / "com.sellee.agent.plist").read_text()
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

    plist = (paths.config_dir() / "com.sellee.agent.plist").read_text()
    assert "<key>PATH</key>" in plist
    assert f"<string>/opt/node-versions/v22/bin:{supervisor.SUPERVISED_PATH}</string>" in plist


def test_a_multi_directory_fragment_reaches_the_jobs_path_whole(xdg_tmp) -> None:
    # On a machine where `node` and `npx` live apart, the recorded value is already a PATH
    # fragment; the job needs every entry, not the first one.
    config.merge_into_file({"node_bin_dir": "/opt/node/bin:/usr/local/npm-global/bin"})
    assert supervisor.install(mode="manual", platform=FakePlatform()) == 0

    plist = (paths.config_dir() / "com.sellee.agent.plist").read_text()
    assert (
        f"<string>/opt/node/bin:/usr/local/npm-global/bin:{supervisor.SUPERVISED_PATH}</string>"
        in plist
    )


def test_no_recorded_node_directory_leaves_the_jobs_path_alone(xdg_tmp) -> None:
    # Nothing recorded means nothing known: naming a PATH here would replace the default with a
    # guess, and a daemon started from a shell already has the right one.
    fake = FakePlatform()
    assert supervisor.install(mode="manual", platform=fake) == 0

    plist = (paths.config_dir() / "com.sellee.agent.plist").read_text()
    assert "<key>PATH</key>" not in plist


# --- mode logic ---------------------------------------------------------------------------


def test_install_manual_places_in_config_dir_and_does_not_register(xdg_tmp) -> None:
    fake = FakePlatform()
    rc = supervisor.install(mode="manual", platform=fake)
    assert rc == 0

    plist = paths.config_dir() / "com.sellee.agent.plist"
    assert plist.exists() and supervisor.MARKER in plist.read_text()
    assert fake.register_calls == []  # manual mode does not auto-start
    assert config.load().daemon_mode == "manual"
    assert paths.current().is_symlink()


def test_install_login_start_places_in_launch_agents_and_registers(xdg_tmp) -> None:
    fake = FakePlatform()
    rc = supervisor.install(mode="login-start", platform=fake)
    assert rc == 0

    plist = paths.launch_agents_dir(platform=fake) / "com.sellee.agent.plist"
    assert plist.exists()
    assert fake.is_registered("com.sellee.agent")
    assert config.load().daemon_mode == "login-start"


def test_flip_moves_the_plist(xdg_tmp) -> None:
    fake = FakePlatform()
    supervisor.install(mode="manual", platform=fake)
    manual_plist = paths.config_dir() / "com.sellee.agent.plist"
    assert manual_plist.exists()

    supervisor.install(mode="login-start", platform=fake)
    login_plist = paths.launch_agents_dir(platform=fake) / "com.sellee.agent.plist"
    assert login_plist.exists()
    assert not manual_plist.exists()  # moved, not duplicated
    assert config.load().daemon_mode == "login-start"


def test_install_refuses_foreign_plist(xdg_tmp) -> None:
    fake = FakePlatform()
    la_dir = paths.launch_agents_dir(platform=fake)
    la_dir.mkdir(parents=True)
    foreign = la_dir / "com.sellee.agent.plist"
    foreign.write_text("<plist>legacy daemon, not ours</plist>")

    rc = supervisor.install(mode="login-start", platform=fake)
    assert rc == 2
    assert foreign.read_text() == "<plist>legacy daemon, not ours</plist>"  # untouched
    assert fake.register_calls == []


def test_start_then_stop(xdg_tmp) -> None:
    fake = FakePlatform()
    supervisor.install(mode="manual", platform=fake)

    assert supervisor.start(platform=fake) == 0
    assert fake.is_registered("com.sellee.agent")
    assert supervisor.start(platform=fake) == 0  # idempotent friendly no-op

    assert supervisor.stop(platform=fake) == 0
    assert not fake.is_registered("com.sellee.agent")


def test_start_without_install_reports_not_installed(xdg_tmp) -> None:
    fake = FakePlatform()
    assert supervisor.start(platform=fake) == 2


def test_uninstall_removes_our_plist(xdg_tmp) -> None:
    fake = FakePlatform()
    supervisor.install(mode="login-start", platform=fake)
    assert supervisor.uninstall(platform=fake) == 0
    assert not (paths.launch_agents_dir(platform=fake) / "com.sellee.agent.plist").exists()
    assert not fake.is_registered("com.sellee.agent")


def test_status_manual_stopped(xdg_tmp) -> None:
    fake = FakePlatform()
    supervisor.install(mode="manual", platform=fake)
    st = supervisor.gather_status(platform=fake)
    assert st.mode == "manual"
    assert st.registered is False
    assert st.label == "com.sellee.agent"


def test_label_override_is_recorded_and_used(xdg_tmp) -> None:
    fake = FakePlatform()
    supervisor.install(mode="login-start", label="com.sellee.agent.dev", platform=fake)
    assert (paths.launch_agents_dir(platform=fake) / "com.sellee.agent.dev.plist").exists()
    assert config.load().daemon_label == "com.sellee.agent.dev"
    assert supervisor.gather_status(platform=fake).label == "com.sellee.agent.dev"


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
    assert (paths.current() / "bin" / "sellee").is_file()


def test_install_refuses_a_real_directory_at_current(xdg_tmp) -> None:
    fake = FakePlatform()
    paths.ensure_runtime_dirs()
    paths.current().mkdir(parents=True)

    assert supervisor.install(mode="manual", platform=fake) == 2
    assert fake.register_calls == []


# --- a container has no job to ask about ------------------------------------------------------


def test_a_containers_status_comes_from_the_process_not_from_a_job(container, xdg_tmp) -> None:
    """Docker is the supervisor here, so `launchctl print` has no counterpart. The instance lock
    names the live holder, which is the same question asked of the thing that can answer it."""
    import os

    paths.ensure_state_dirs()
    paths.lock_path().write_text(str(os.getpid()))

    status = supervisor.gather_status()
    assert status.mode == "container"
    assert status.registered is True


def test_a_container_whose_worker_died_reads_as_stopped(container, xdg_tmp) -> None:
    paths.ensure_state_dirs()
    # A pid that cannot be running: the lock body outlived its holder.
    paths.lock_path().write_text("999999999")
    assert supervisor.gather_status().registered is False


def test_a_container_status_never_resolves_a_host_platform(container, xdg_tmp, monkeypatch) -> None:
    """get_platform() would hand back a ContainerPlatform that refuses every supervisor question,
    so the branch has to come first rather than be caught afterwards."""

    def explode():
        raise AssertionError("the platform seam was resolved in container mode")

    monkeypatch.setattr(supervisor, "get_platform", explode)
    assert supervisor.gather_status().mode == "container"


def test_gather_status_reads_channel_adapter_when_bound(store, xdg_tmp, monkeypatch) -> None:
    paths.ensure_state_dirs()
    store.arm_bind("test_bot", "nonce1", adapter="discord")
    store.complete_bind(12345, 1, nonce=store.get_channel()["bind_nonce"])  # chat_id, update_offset
    monkeypatch.setattr(supervisor.paths, "sellee_db", lambda: store._db.path)

    status = supervisor.gather_status()
    assert status.channel_bound is True
    assert status.channel_adapter == "discord"


def test_gather_status_channel_adapter_none_when_unbound(store, xdg_tmp, monkeypatch) -> None:
    paths.ensure_state_dirs()
    monkeypatch.setattr(supervisor.paths, "sellee_db", lambda: store._db.path)

    status = supervisor.gather_status()
    assert status.channel_bound is False
    assert status.channel_adapter is None


# --- what "running" is allowed to mean -----------------------------------------------------------


def _status(**kw):
    from sellee.supervisor import Status

    base = dict(
        label="com.sellee.agent",
        mode="login-start",
        registered=True,
        heartbeat_age_sec=1.0,
        recent_events=[],
        channel_bound=True,
        channel_adapter="telegram",
        paused=False,
        queued_notices=0,
        process_alive=True,
    )
    base.update(kw)
    return Status(**base)


def _state_line(capsys, monkeypatch, st) -> str:
    from sellee import supervisor

    monkeypatch.setattr(supervisor, "gather_status", lambda **kw: st)
    supervisor.status()
    return [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("state:")][0]


def test_a_registered_job_with_no_process_is_not_called_running(capsys, monkeypatch) -> None:
    """A clean stop leaves the job registered and nothing running. Reported as "running", that is
    how a dead daemon goes unnoticed — a seller taps Resume in chat, nothing is there to receive
    it, and the button neither works nor says anything."""
    line = _state_line(capsys, monkeypatch, _status(process_alive=False))

    assert "NOT running" in line
    assert "sellee daemon start" in line


def test_a_process_that_has_stopped_ticking_is_not_called_running(capsys, monkeypatch) -> None:
    """Alive and wedged is a different thing from stopped, and wants a different answer from
    whoever is reading."""
    from sellee import supervisor

    line = _state_line(
        capsys,
        monkeypatch,
        _status(heartbeat_age_sec=supervisor._WEDGED_AFTER_SEC + 60),
    )

    assert "not ticking" in line


def test_a_live_ticking_daemon_is_running(capsys, monkeypatch) -> None:
    assert _state_line(capsys, monkeypatch, _status()) == "state:     running"


def test_pause_and_resume_reach_the_store_without_the_daemon(xdg_tmp, monkeypatch) -> None:
    """The door that exists for the case the chat button cannot cover: a tap only arrives if
    something is alive to receive it, and the one control whose whole job is getting out of a stuck
    state must not need the stuck thing to be working."""
    from sellee import cli, migrations, paths
    from sellee.db import Database
    from sellee.store import Store

    paths.ensure_state_dirs()
    data = Database(paths.data_dir() / "sellee.db")
    migrations.run_startup_migrations(
        data_db=data,
        events_db=Database(paths.events_db()),
        backups_dir=paths.state_dir() / "backups",
        backups_keep=2,
    )

    assert cli.main(["sellee", "pause"]) == 0
    assert Store(Database(paths.data_dir() / "sellee.db")).is_paused() is True

    assert cli.main(["sellee", "resume"]) == 0
    assert Store(Database(paths.data_dir() / "sellee.db")).is_paused() is False
