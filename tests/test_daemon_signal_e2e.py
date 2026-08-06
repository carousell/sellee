"""End-to-end: a foreground daemon subprocess stops cleanly, by signal and by control route.

The signal is what launchd delivers; the route is what every platform can reach, and the only
thing that stops a daemon on Windows. Both must reach the same drain: the stop event, then the
lanes settling, `daemon.stop` recorded, the lock body cleared, and exit 0.
"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from selly_agent import db

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "bin" / "selly-agent"


def _env(tmp_path) -> dict:
    env = dict(os.environ)
    env.update(
        HOME=str(tmp_path / "home"),
        XDG_DATA_HOME=str(tmp_path / "data"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
    )
    return env


def _write_config(tmp_path, *, http_port: int = 0) -> None:
    cfg_dir = tmp_path / "config" / "selly-agent"
    cfg_dir.mkdir(parents=True)
    # http_port 0 → an ephemeral port, so two daemon subprocesses never collide on a fixed port.
    # A test that has to address the daemon names one instead, having found a free one first.
    #
    # The browser is pointed at names that cannot resolve, so the lanes report it unavailable and
    # this stays a test about the drain. Left to find the host's own tooling, the daemon launches
    # whatever Chrome and npx are installed and waits on them — which is why this passed on a
    # machine with neither and failed on CI runners that have both.
    (cfg_dir / "config.json").write_text(
        json.dumps(
            {
                "tick_interval_sec": 0.3,
                "http_port": http_port,
                "chrome_bin": str(tmp_path / "no-such-chrome"),
                "playwright_mcp_cmd": [str(tmp_path / "no-such-npx")],
            }
        )
    )


def _daemon(tmp_path, extra_env=None):
    """Start a foreground daemon, with its output going to a file rather than a pipe.

    A pipe nobody drains is a deadlock waiting for a talkative run: the daemon blocks on write
    once the buffer fills, which is 16KB on macOS against 64KB on Linux — so the same daemon
    wedges on one and not the other. A file has no such limit, and it is readable afterwards,
    which a failed run needs.
    """
    env = _env(tmp_path)
    # So a daemon that will not go can be asked where it is: with this set, SIGABRT dumps every
    # thread's stack to stderr, which _exits collects. A hang here is otherwise only a timeout.
    env["PYTHONFAULTHANDLER"] = "1"
    env.update(extra_env or {})
    log_path = tmp_path / "daemon-output.log"
    handle = open(log_path, "wb")  # noqa: SIM115 — closed by the caller's finally
    proc = subprocess.Popen(
        [sys.executable, str(LAUNCHER), "daemon", "run"],
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    proc._selly_log = log_path  # type: ignore[attr-defined]
    proc._selly_handle = handle  # type: ignore[attr-defined]
    return proc


def _daemon_output(proc) -> str:
    """What the daemon said, for a failure message that would otherwise name only a timeout."""
    try:
        return pathlib.Path(proc._selly_log).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "<no output captured>"


def _exits(proc, timeout: float) -> int:
    """Wait for exit, reporting what the daemon said when it does not come.

    Without this a stop that hangs is only ever a TimeoutExpired naming the command — true, and
    no help at all in working out which lane was still holding the drain.
    """
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stacks = _stack_dump(proc)
        raise AssertionError(
            f"daemon did not exit within {timeout}s.\nIts output:\n{_daemon_output(proc)}\n"
            f"Where it was stuck:\n{stacks}"
        ) from None


def _stack_dump(proc) -> str:
    """Every thread's stack, from a daemon that has stopped responding.

    PYTHONFAULTHANDLER makes SIGABRT dump them; the process is already being abandoned at this
    point, so killing it to find out where it stopped costs nothing.
    """
    if proc.poll() is not None:
        return "<already exited>"
    before = len(_daemon_output(proc))
    proc.send_signal(signal.SIGABRT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    return _daemon_output(proc)[before:] or "<no dump>"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _events_db(tmp_path) -> Path:
    return tmp_path / "state" / "selly-agent" / "events.db"


def _kinds(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db.read_only_uri(db_path), uri=True)
    try:
        return [r[0] for r in conn.execute("SELECT kind FROM events ORDER BY seq")]
    except sqlite3.OperationalError:
        return []  # events.db exists but the migration hasn't created the table yet
    finally:
        conn.close()


def _wait_for(predicate, timeout=15.0, interval=0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX signal semantics")
def test_sigterm_stops_cleanly(tmp_path) -> None:
    (tmp_path / "home").mkdir()
    _write_config(tmp_path)

    proc = _daemon(tmp_path)
    try:
        started = _wait_for(
            lambda: _events_db(tmp_path).exists() and "daemon.start" in _kinds(_events_db(tmp_path))
        )
        assert started, f"daemon never recorded daemon.start:\n{_daemon_output(proc)}"

        proc.send_signal(signal.SIGTERM)
        assert _exits(proc, 10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    kinds = _kinds(_events_db(tmp_path))
    assert "daemon.stop" in kinds

    lock_body = (tmp_path / "state" / "selly-agent" / "daemon.lock").read_text()
    assert lock_body == ""  # clean shutdown cleared our own holder record


def test_the_shutdown_route_stops_cleanly(tmp_path) -> None:
    """The same drain, triggered without a signal — the only stop available on Windows."""
    (tmp_path / "home").mkdir()
    port = _free_port()
    _write_config(tmp_path, http_port=port)

    proc = _daemon(tmp_path)
    try:
        started = _wait_for(
            lambda: _events_db(tmp_path).exists() and "daemon.start" in _kinds(_events_db(tmp_path))
        )
        assert started, f"daemon never recorded daemon.start:\n{_daemon_output(proc)}"

        token = (tmp_path / "config" / "selly-agent" / "mcp_token").read_text().strip()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/control/shutdown",
            data=b"{}",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "Origin": "http://127.0.0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                assert response.status == 202
                assert json.loads(response.read())["pid"] == proc.pid
        except urllib.error.URLError as exc:
            raise AssertionError(
                f"the control route did not answer ({exc}).\nIts output:\n"
                f"{_daemon_output(proc)}\nWhere it was stuck:\n{_stack_dump(proc)}"
            ) from None

        assert _exits(proc, 15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    assert "daemon.stop" in _kinds(_events_db(tmp_path))
    assert (tmp_path / "state" / "selly-agent" / "daemon.lock").read_text() == ""


def test_the_shutdown_route_needs_the_attended_token(tmp_path) -> None:
    (tmp_path / "home").mkdir()
    port = _free_port()
    _write_config(tmp_path, http_port=port)

    proc = _daemon(tmp_path)
    try:
        assert _wait_for(
            lambda: _events_db(tmp_path).exists() and "daemon.start" in _kinds(_events_db(tmp_path))
        )
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/control/shutdown",
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer not-the-token", "Origin": "http://127.0.0.1"},
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        assert caught.value.code == 401
        # Still running: an unauthorized request must not be a stop.
        assert proc.poll() is None
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX signal semantics")
def test_second_instance_exits_zero(tmp_path) -> None:
    (tmp_path / "home").mkdir()
    _write_config(tmp_path)
    env = _env(tmp_path)

    first = _daemon(tmp_path)
    try:
        assert _wait_for(
            lambda: _events_db(tmp_path).exists() and "daemon.start" in _kinds(_events_db(tmp_path))
        )
        # a second instance collides with the live holder and must exit 0 (INV-27)
        second = subprocess.run(
            [sys.executable, str(LAUNCHER), "daemon", "run", "--once"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert second.returncode == 0
        assert "already running" in second.stdout + second.stderr
    finally:
        first.send_signal(signal.SIGTERM)
        try:
            first.wait(timeout=10)
        except subprocess.TimeoutExpired:
            first.kill()
            first.wait()
