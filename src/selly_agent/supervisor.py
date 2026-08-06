"""Supervisor integration — install/start/stop/status/uninstall (OS-agnostic orchestration).

The OS-specific bits (rendering the job definition, registering it) live behind the platform seam;
everything here (mode logic, config recording, ours-vs-foreign refusal, the confirmed stop) is
portable. On macOS start-on-login is expressed by plist *placement* rather than a RunAtLoad toggle:
login-start mode places it where launchd auto-loads it at login, manual mode keeps it in the config
dir and registers it on demand. Crash keep-alive is identical in both modes once registered.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from selly_agent import config, control, heartbeat, host, lock, paths, proc_tree, secrets
from selly_agent.db import connect_reader
from selly_agent.events import query_events
from selly_agent.installer import materialize
from selly_agent.platform import Platform, get_platform
from selly_agent.platform.base import MARKER, RegistrationError

log = logging.getLogger(__name__)

LOGIN_START = "login-start"
MANUAL = "manual"


def _resolve_platform(platform: Platform | None) -> Platform:
    return platform if platform is not None else get_platform()


def _resolve_label(platform: Platform, label: str | None) -> str:
    if label:
        return label
    return config.load().daemon_label or platform.default_label()


def _is_ours(path: Path, platform: Platform) -> bool:
    """Whether a definition file was written by us — read in the encoding we write.

    A foreign file that does not decode is simply not ours; on Windows the definition is UTF-16,
    which the platform-default read would turn into NUL-interleaved garbage the marker never
    matches.
    """
    try:
        return MARKER in path.read_text(encoding=platform.definition_encoding)
    except (OSError, UnicodeError):
        return False


def _default_supervised_path() -> str:
    """What a supervised job's PATH is when its definition names none.

    Spelled out because naming a PATH in the definition replaces this rather than extending it —
    and because the installer's gates verify the browser server can be spawned under exactly this,
    plus the recorded fragment.
    """
    if host.windows():
        root = os.environ.get("SystemRoot", r"C:\Windows")
        dirs = (rf"{root}\system32", root, rf"{root}\System32\Wbem")
    else:
        dirs = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
    return os.pathsep.join(dirs)


SUPERVISED_PATH = _default_supervised_path()


def _job_environment() -> dict:
    """The environment the supervised daemon is given.

    A supervised job inherits nothing from the shell that installed it, so everything the daemon
    cannot work without is pinned into its definition. Two things qualify. The XDG overrides that
    were in force shaped every path this install provisioned, and a daemon resolving different
    ones would be reading a different machine's state. And the recorded node path fragment — one or
    more directories: neither `node` nor `npx` is on the default PATH above (a version manager keeps
    them somewhere only an interactive shell knows about), so without it the browser server cannot
    be spawned at all.
    """
    env = dict(paths.xdg_overrides())
    node_bin_dir = config.load().node_bin_dir
    if node_bin_dir:
        env["PATH"] = f"{node_bin_dir}{os.pathsep}{SUPERVISED_PATH}"
    # Everything the daemon writes and reads is UTF-8; on Windows the interpreter's text-mode
    # default is a legacy code page unless told otherwise. A no-op where UTF-8 already is the
    # locale, so it is pinned everywhere rather than per-OS.
    env["PYTHONUTF8"] = "1"
    return env


def _environment_file(definition: Path) -> Path:
    """The companion file carrying the job environment, for a platform whose definition cannot."""
    return definition.with_name(definition.stem + ".env.json")


def _plist_locations(platform: Platform, label: str) -> dict:
    filename = platform.supervisor_filename(label)
    return {
        LOGIN_START: paths.launch_agents_dir(platform=platform) / filename,
        MANUAL: paths.config_dir() / filename,
    }


def _find_installed(platform: Platform, label: str) -> Path | None:
    for location in _plist_locations(platform, label).values():
        if location.exists() and _is_ours(location, platform):
            return location
    return None


def install(*, mode: str, label: str | None = None, platform: Platform | None = None) -> int:
    platform = _resolve_platform(platform)
    # The recorded label, not the default one — otherwise an install with a custom label writes
    # and registers a *second* job under the default name, leaving the original loaded and two
    # daemons writing one database.
    label = _resolve_label(platform, label)
    locations = _plist_locations(platform, label)

    # Refuse if a foreign plist with our label already occupies either target location.
    for location in locations.values():
        if location.exists() and not _is_ours(location, platform):
            print(
                f"refusing to install: a job definition labelled {label!r} at {location} was not "
                f"written by selly-agent — remove it first (never replacing a foreign daemon's).",
                file=sys.stderr,
            )
            return 2

    try:
        materialize.ensure_current(materialize.source_tree())
    except materialize.LayoutError as exc:
        print(f"refusing to install: {exc.message}", file=sys.stderr)
        if exc.fix:
            print(exc.fix, file=sys.stderr)
        return 2

    dest = locations[mode]
    environment = _job_environment()
    launcher_args = ["daemon", "run"]
    if platform.environment_file:
        # The definition format cannot carry environment variables, so the launcher applies them
        # from a companion file named in the job's own arguments.
        launcher_args[:0] = ["--env-file", str(_environment_file(dest))]
    program_args = [
        str(materialize.supervised_interpreter()),
        *platform.supervised_interpreter_flags,
        str(paths.current() / "bin" / "selly-agent"),
        *launcher_args,
    ]
    plist_text = platform.render_supervisor(
        label=label,
        program_args=program_args,
        stdout_path=paths.logs_dir() / "agent.out.log",
        stderr_path=paths.logs_dir() / "agent.err.log",
        marker=MARKER,
        environment=environment,
        start_at_login=mode == LOGIN_START,
        working_dir=paths.current(),
    )

    # Remove any of our own plists from the other location (a mode flip moves the plist).
    for other_mode, location in locations.items():
        if other_mode != mode and location.exists() and _is_ours(location, platform):
            if platform.is_registered(label):
                platform.unregister(label)
            location.unlink()
            _environment_file(location).unlink(missing_ok=True)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(plist_text, encoding=platform.definition_encoding)
    if platform.environment_file:
        _environment_file(dest).write_text(
            json.dumps(environment, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    config.merge_into_file({"daemon_mode": mode, "daemon_label": label})

    if mode == LOGIN_START:
        try:
            platform.register(dest)
        except RegistrationError as exc:
            print(f"selly-agent: {exc}", file=sys.stderr)
            return 2
        print(f"installed (login-start) and started; definition at {dest}")
    else:
        print(
            f"installed (manual mode); start with: selly-agent daemon start\ndefinition at {dest}"
        )
    return 0


def start(*, label: str | None = None, platform: Platform | None = None) -> int:
    platform = _resolve_platform(platform)
    label = _resolve_label(platform, label)
    plist = _find_installed(platform, label)
    if plist is None:
        print(
            "not installed — run: selly-agent daemon install --login-start|--manual",
            file=sys.stderr,
        )
        return 2
    if platform.is_registered(label):
        print("already running")
        return 0
    try:
        platform.register(plist)
    except RegistrationError as exc:
        print(f"selly-agent: {exc}", file=sys.stderr)
        return 2
    print("started")
    return 0


# How long the daemon gets to finish what it is doing once asked. A pass in flight is the long
# case; the drain does not wait for one, so this covers the lanes settling and the files closing.
STOP_TIMEOUT_SEC = 30.0
_STOP_POLL_SEC = 0.2
# After the kill: only long enough to confirm it landed.
_FORCED_EXIT_WAIT_SEC = 10.0


def daemon_pid() -> int | None:
    """The PID of the running daemon, from the instance lock; None when nothing holds it."""
    pid = lock.read_holder_pid(paths.lock_path())
    return pid if lock.is_pid_alive(pid) else None


def shutdown(
    *, label: str | None = None, platform: Platform | None = None, timeout_sec: float | None = None
) -> bool:
    """Take the daemon down and wait until its process is gone. True when nothing is running.

    The job is deregistered first so the supervisor cannot restart what is about to stop, then the
    daemon is asked over its control route to drain and exit. Asked rather than signalled because
    nothing delivers a signal on Windows — and because callers that go on to replace files the
    daemon holds open need a stop that is settled, not merely requested.

    A daemon that has not gone by the deadline is wedged rather than busy — the drain does not wait
    on a pass — so it is killed outright, tree and all. That is safe for the databases, which are
    written under WAL and recovered by whoever opens them next, and it is what keeps one stuck
    daemon from blocking every future update. False means even that did not work.
    """
    platform = _resolve_platform(platform)
    label = _resolve_label(platform, label)
    if platform.is_registered(label):
        platform.unregister(label)

    pid = daemon_pid()
    if pid is None:
        return True
    token = secrets.read_mcp_token()
    if token:
        try:
            control.post(config.load().http_port, token, "/control/shutdown", {})
        except control.DaemonUnreachable:
            # Already going, or already gone. The wait below is the answer either way.
            pass

    if _wait_for_exit(pid, STOP_TIMEOUT_SEC if timeout_sec is None else timeout_sec):
        return True
    log.warning("the worker (pid %s) did not stop when asked; killing it", pid)
    if not _is_the_daemon_we_recorded(pid):
        # The lock says this pid, but the OS says it is not the process that wrote our heartbeat.
        # A stale lock plus a reused number is enough to point kill_tree at a stranger's process
        # tree, so an identity we cannot establish means we do not kill.
        log.error(
            "refusing to kill pid %s: it is not the worker this install recorded. "
            "Check it by hand before starting again.",
            pid,
        )
        return False
    # The kill takes the daemon's whole tree — including a Chrome this daemon spawned. That is
    # accepted: it is the agent's own Chrome on a dedicated profile, its sessions persist on
    # disk, and the next launch clears the stale locks a killed Chrome leaves. (One this daemon
    # merely re-attached to is not among its children and survives.)
    proc_tree.kill_tree(pid)
    return _wait_for_exit(pid, _FORCED_EXIT_WAIT_SEC)


def _is_the_daemon_we_recorded(pid: int) -> bool:
    """Whether `pid` is still the process the heartbeat was written by.

    A wedged-but-alive daemon keeps writing heartbeats, and one that died before its first tick
    has nothing left to kill — so refusing when the record is missing costs nothing real, and it
    is the only answer that cannot kill the wrong process.
    """
    record = heartbeat.read(paths.heartbeat_path())
    if not record or record.get("pid") != pid:
        return False
    created = record.get("created")
    if not isinstance(created, (int, float)):
        return False  # an older install's record, written before creation time was kept
    return proc_tree.is_recorded_process(pid, created)


def _wait_for_exit(pid: int, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec
    while lock.is_pid_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_STOP_POLL_SEC)
    return True


def stop(*, label: str | None = None, platform: Platform | None = None) -> int:
    platform = _resolve_platform(platform)
    label = _resolve_label(platform, label)
    pid = daemon_pid()
    running = platform.is_registered(label) or pid is not None
    if not shutdown(label=label, platform=platform):
        # The pid is the one read before the stop: re-reading here could name None for a daemon
        # that died between the failed confirmation window and this message.
        print(
            f"the worker (pid {pid}) is still running after being asked to stop and then "
            "killed. Check `selly-agent logs`.",
            file=sys.stderr,
        )
        return 1
    print("stopped" if running else "not running")
    return 0


def uninstall(*, label: str | None = None, platform: Platform | None = None) -> int:
    platform = _resolve_platform(platform)
    label = _resolve_label(platform, label)
    # Drained rather than just deregistered: the files about to be removed are the ones it has open.
    try:
        shutdown(label=label, platform=platform)
    except RegistrationError as exc:
        # Best-effort here, unlike `stop`: an uninstall that stopped at the first thing it
        # could not remove would leave more behind than one that carries on and says so.
        print(f"could not deregister the worker: {exc}", file=sys.stderr)
    for location in _plist_locations(platform, label).values():
        if location.exists() and _is_ours(location, platform):
            location.unlink()
            _environment_file(location).unlink(missing_ok=True)
    if platform.owns_job_directory:
        # A directory we created for this purpose, so an empty one left behind is our litter.
        with contextlib.suppress(OSError):
            paths.launch_agents_dir(platform=platform).rmdir()
    print("uninstalled supervisor")
    return 0


@dataclass
class Status:
    label: str
    mode: str
    registered: bool
    heartbeat_age_sec: float | None
    recent_events: list
    channel_bound: bool
    paused: bool
    queued_notices: int


def _channel_snapshot() -> dict:
    """Read channel-bound / paused / queued-notice state from selly.db over a read-only connection
    (the daemon's WAL DB allows a concurrent reader). Degrades to all-false when the DB or the
    channel tables are not present yet, so `daemon status` never errors on a fresh install."""
    import sqlite3

    db = paths.selly_db()
    default = {"channel_bound": False, "paused": False, "queued_notices": 0}
    if not db.exists():
        return default
    conn = connect_reader(db)
    try:
        ch = conn.execute("SELECT chat_id FROM channel WHERE id = 1").fetchone()
        ctrl = conn.execute("SELECT paused FROM control WHERE id = 1").fetchone()
        notices = conn.execute(
            "SELECT COUNT(*) AS n FROM notices WHERE status = 'queued'"
        ).fetchone()
        return {
            "channel_bound": ch is not None and ch["chat_id"] is not None,
            "paused": bool(ctrl["paused"]) if ctrl else False,
            "queued_notices": notices["n"],
        }
    except sqlite3.DatabaseError:
        # Not migrated yet, or not readable at all. Both are things `daemon status` and the
        # healthcheck have to survive: a damaged database is precisely when someone needs a
        # status report, and crashing here would take the whole report with it.
        return default
    finally:
        conn.close()


def gather_status(*, label: str | None = None, platform: Platform | None = None) -> Status:
    platform = _resolve_platform(platform)
    cfg = config.load()
    label = label or cfg.daemon_label or platform.default_label()

    hb_age = heartbeat.age(paths.heartbeat_path())

    recent: list = []
    if paths.events_db().exists():
        conn = connect_reader(paths.events_db())
        try:
            all_events = query_events(conn)
            recent = all_events[-5:]
        finally:
            conn.close()

    snap = _channel_snapshot()
    return Status(
        label=label,
        mode=cfg.daemon_mode,
        registered=platform.is_registered(label),
        heartbeat_age_sec=hb_age,
        recent_events=recent,
        channel_bound=snap["channel_bound"],
        paused=snap["paused"],
        queued_notices=snap["queued_notices"],
    )


def status(*, label: str | None = None, platform: Platform | None = None) -> int:
    st = gather_status(label=label, platform=platform)
    if st.registered:
        state = "running"
    elif st.mode == MANUAL:
        state = "stopped (manual mode — selly-agent daemon start)"
    else:
        state = "stopped"
    print(f"label:     {st.label}")
    print(f"mode:      {st.mode}")
    print(f"state:     {state}")
    if st.heartbeat_age_sec is None:
        print("heartbeat: none")
    else:
        print(f"heartbeat: {st.heartbeat_age_sec:.0f}s ago")
    print(f"channel:   {'bound' if st.channel_bound else 'not connected'}")
    print(f"paused:    {'yes' if st.paused else 'no'}")
    print(f"notices:   {st.queued_notices} queued")
    if st.recent_events:
        print("recent events:")
        for ev in st.recent_events:
            print(f"  {ev.kind}")
    return 0
