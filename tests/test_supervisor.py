"""launchd integration: golden plist render, and mode logic with launchctl stubbed."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from selly_agent import config, heartbeat, paths, pointer, supervisor
from selly_agent.installer import materialize
from selly_agent.platform.macos import MacOSPlatform
from selly_agent.platform.windows import WindowsPlatform

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
        # Plain strings, not Path: this golden describes a macOS plist, and a Path would be
        # re-spelled in the running host's separator before it reached the render.
        stdout_path="/state/logs/agent.out.log",
        stderr_path="/state/logs/agent.err.log",
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

    assert materialize.install_interpreter() == interpreter

    fake = FakePlatform()
    assert supervisor.install(mode="manual", platform=fake) == 0
    plist = (paths.config_dir() / "com.selly.agent.plist").read_text()
    assert f"<string>{interpreter}</string>" in plist


def test_the_job_names_the_interpreter_through_current_not_a_version(xdg_tmp, tree) -> None:
    # Named through the swap point, so the definition does not go stale the moment a version is
    # replaced underneath it — and so an update's re-register writes the same path back.
    materialize.install_version(tree, "1.0.0")
    named = str(materialize.install_interpreter())
    assert named.startswith(str(paths.current()))
    assert "versions" not in named


def test_a_checkout_without_a_venv_still_gets_a_startable_job(xdg_tmp) -> None:
    # `./setup --dev` before a bootstrap: naming an interpreter that does not exist would give a
    # job that fails to start with nothing saying why.
    assert materialize.install_interpreter() == Path(os.path.realpath(sys.executable))


def test_the_plist_puts_the_recorded_node_directory_on_the_jobs_path(xdg_tmp) -> None:
    # A supervised job's PATH holds none of a version manager's directories, so `npx` — and so the
    # whole browser layer — is unreachable unless the installer's answer is carried here.
    config.merge_into_file({"node_bin_dir": "/opt/node-versions/v22/bin"})
    fake = FakePlatform()
    assert supervisor.install(mode="manual", platform=fake) == 0

    plist = (paths.config_dir() / "com.selly.agent.plist").read_text()
    assert "<key>PATH</key>" in plist
    assert (
        f"<string>/opt/node-versions/v22/bin{os.pathsep}{supervisor.SUPERVISED_PATH}</string>"
        in plist
    )


def test_a_multi_directory_fragment_reaches_the_jobs_path_whole(xdg_tmp) -> None:
    # On a machine where `node` and `npx` live apart, the recorded value is already a PATH
    # fragment; the job needs every entry, not the first one.
    config.merge_into_file({"node_bin_dir": "/opt/node/bin:/usr/local/npm-global/bin"})
    assert supervisor.install(mode="manual", platform=FakePlatform()) == 0

    plist = (paths.config_dir() / "com.selly.agent.plist").read_text()
    assert (
        f"<string>/opt/node/bin:/usr/local/npm-global/bin{os.pathsep}"
        f"{supervisor.SUPERVISED_PATH}</string>" in plist
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
    assert pointer.is_pointer(paths.current())


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
    assert pointer.is_pointer(paths.current())
    assert (paths.current() / "bin" / "selly-agent").is_file()


def test_install_refuses_a_real_directory_at_current(xdg_tmp) -> None:
    fake = FakePlatform()
    paths.ensure_runtime_dirs()
    paths.current().mkdir(parents=True)

    assert supervisor.install(mode="manual", platform=fake) == 2
    assert fake.register_calls == []


def test_uninstall_leaves_the_system_job_directory_standing(xdg_tmp) -> None:
    """It holds every application's job on macOS, so removing it — even when ours was the only file
    in it — would be taking something that is not ours."""
    fake = FakePlatform()
    assert supervisor.install(mode="login-start", platform=fake) == 0
    job_dir = paths.launch_agents_dir(platform=fake)
    assert job_dir.is_dir()

    supervisor.uninstall(platform=fake)

    assert job_dir.is_dir()
    assert not (job_dir / "com.selly.agent.plist").exists()


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


def _recorded_daemon(monkeypatch, pid: int) -> None:
    """A heartbeat naming `pid`, and an OS that agrees the process holding it is that one.

    Without this the kill path refuses, which is the point of it: a lock naming a pid the OS has
    since handed to someone else must not steer kill_tree into a stranger's process tree.
    """
    paths.ensure_state_dirs()
    monkeypatch.setattr(supervisor.proc_tree, "creation_time", lambda _pid: 1000.0)
    heartbeat.write(paths.heartbeat_path(), pid)
    monkeypatch.setattr(
        supervisor.proc_tree, "is_recorded_process", lambda p, created, **kw: created == 1000.0
    )


def test_a_daemon_that_ignores_the_request_is_killed(xdg_tmp, monkeypatch) -> None:
    """Past the deadline it is wedged rather than busy, and one stuck daemon must not block every
    future update. The databases are written under WAL, so a kill costs a recovery, not data."""
    fake = FakePlatform()
    killed = []
    monkeypatch.setattr(supervisor.secrets, "read_mcp_token", lambda: None)
    monkeypatch.setattr(supervisor, "STOP_TIMEOUT_SEC", 0.0)
    monkeypatch.setattr(supervisor.proc_tree, "kill_tree", lambda pid: killed.append(pid) or True)
    _pretend_holder(monkeypatch, 4242, True, True, False)
    _recorded_daemon(monkeypatch, 4242)

    assert supervisor.shutdown(platform=fake) is True
    assert killed == [4242]


def test_a_pid_the_daemon_no_longer_owns_is_never_killed(xdg_tmp, monkeypatch) -> None:
    """PIDs are reused, and the instance lock's stale window is unbounded in manual mode. The
    stray ledger has always checked creation time before killing; the daemon's own lock did not,
    so a wedged-stop could take out whatever process inherited the number."""
    fake = FakePlatform()
    killed = []
    monkeypatch.setattr(supervisor.secrets, "read_mcp_token", lambda: None)
    monkeypatch.setattr(supervisor, "STOP_TIMEOUT_SEC", 0.0)
    monkeypatch.setattr(supervisor.proc_tree, "kill_tree", lambda pid: killed.append(pid) or True)
    _pretend_holder(monkeypatch, 4242, True, True, False)
    _recorded_daemon(monkeypatch, 4242)
    # The OS says the process now holding 4242 started at some other time than we recorded.
    monkeypatch.setattr(supervisor.proc_tree, "is_recorded_process", lambda p, created, **kw: False)

    assert supervisor.shutdown(platform=fake) is False
    assert killed == []


def test_a_kill_is_refused_when_no_heartbeat_names_the_pid(xdg_tmp, monkeypatch) -> None:
    """No record means no identity. A wedged-but-alive daemon writes heartbeats, and one that
    died before its first tick has nothing left to kill, so refusing costs nothing real."""
    fake = FakePlatform()
    killed = []
    monkeypatch.setattr(supervisor.secrets, "read_mcp_token", lambda: None)
    monkeypatch.setattr(supervisor, "STOP_TIMEOUT_SEC", 0.0)
    monkeypatch.setattr(supervisor.proc_tree, "kill_tree", lambda pid: killed.append(pid) or True)
    _pretend_holder(monkeypatch, 4242, True, True, False)

    assert supervisor.shutdown(platform=fake) is False
    assert killed == []


def test_a_daemon_that_survives_even_the_kill_is_not_reported_as_stopped(
    xdg_tmp, monkeypatch
) -> None:
    fake = FakePlatform()
    monkeypatch.setattr(supervisor.secrets, "read_mcp_token", lambda: None)
    monkeypatch.setattr(supervisor, "STOP_TIMEOUT_SEC", 0.0)
    monkeypatch.setattr(supervisor, "_FORCED_EXIT_WAIT_SEC", 0.0)
    monkeypatch.setattr(supervisor.proc_tree, "kill_tree", lambda _pid: False)
    _pretend_holder(monkeypatch, 4242, True)
    _recorded_daemon(monkeypatch, 4242)

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


# --- ours-marker reading ------------------------------------------------------------------


def test_ours_check_reads_the_definition_in_the_platform_encoding(tmp_path) -> None:
    """The Windows definition is UTF-16. A platform-default read decodes it to NUL-interleaved
    garbage the marker never matches — after which every lifecycle operation refuses to
    recognise the file it wrote itself."""

    class Utf16Platform(FakePlatform):
        definition_encoding = "utf-16"

    path = tmp_path / "SellyAgent.xml"
    path.write_text(f"<Description>{supervisor.MARKER}</Description>", encoding="utf-16")

    assert supervisor._is_ours(path, Utf16Platform())


def test_a_file_that_does_not_decode_is_not_ours(tmp_path) -> None:
    path = tmp_path / "SellyAgent.xml"
    path.write_bytes(b"\xff\xfe\x00")  # a truncated UTF-16 stream no encoding accepts

    assert not supervisor._is_ours(path, FakePlatform())


# --- environment delivery through a companion file ----------------------------------------


class FakeWindowsPlatform(WindowsPlatform):
    """The Windows platform with its schtasks calls recorded in-memory instead of executed."""

    def __init__(self):
        self.registered_labels: set[str] = set()

    def register(self, config_path: Path) -> None:
        self.registered_labels.add(Path(config_path).stem)

    def unregister(self, label: str) -> None:
        self.registered_labels.discard(label)

    def is_registered(self, label: str) -> bool:
        return label in self.registered_labels


def test_a_definition_that_cannot_carry_environment_gets_a_companion_file(xdg_tmp) -> None:
    """Task Scheduler XML has no environment element, so the pinned environment must arrive
    through the companion file the action's --env-file argument names — dropping it silently is
    how the daemon ends up resolving a different world's paths than the installer provisioned."""
    fake = FakeWindowsPlatform()
    assert supervisor.install(mode="manual", platform=fake) == 0

    dest = paths.config_dir() / "SellyAgent.xml"
    companion = paths.config_dir() / "SellyAgent.env.json"
    definition = dest.read_text(encoding="utf-16")
    assert "--env-file" in definition
    assert companion.name in definition

    environment = json.loads(companion.read_text(encoding="utf-8"))
    assert environment["PYTHONUTF8"] == "1"
    assert environment["XDG_STATE_HOME"] == os.environ["XDG_STATE_HOME"]


def test_the_supervised_interpreter_is_started_in_utf8_mode(xdg_tmp) -> None:
    """PYTHONUTF8 in the companion file cannot do this: the launcher reads that file, and by then
    the interpreter's text-mode defaults are already set from the machine's code page. Only a flag
    on the command line lands before startup, so the daemon reads the skill files it ships with."""
    fake = FakeWindowsPlatform()
    assert supervisor.install(mode="manual", platform=fake) == 0

    definition = (paths.config_dir() / "SellyAgent.xml").read_text(encoding="utf-16")
    arguments = definition.split("<Arguments>")[1].split("</Arguments>")[0]
    assert arguments.startswith("-X utf8 ")
    # ...and before the launcher script, or the interpreter treats it as one of its arguments.
    assert arguments.index("-X utf8") < arguments.index("selly-agent")


def test_uninstall_takes_the_companion_file_with_the_definition(xdg_tmp) -> None:
    fake = FakeWindowsPlatform()
    assert supervisor.install(mode="manual", platform=fake) == 0
    companion = paths.config_dir() / "SellyAgent.env.json"
    assert companion.exists()

    assert supervisor.uninstall(platform=fake) == 0

    assert not companion.exists()
    assert not (paths.config_dir() / "SellyAgent.xml").exists()
