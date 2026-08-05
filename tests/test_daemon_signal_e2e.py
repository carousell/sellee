"""End-to-end: a foreground daemon subprocess stops cleanly on SIGTERM (mirrors launchd)."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
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


def _write_config(tmp_path) -> None:
    cfg_dir = tmp_path / "config" / "selly-agent"
    cfg_dir.mkdir(parents=True)
    # http_port 0 → an ephemeral port, so two daemon subprocesses never collide on a fixed port.
    (cfg_dir / "config.json").write_text(json.dumps({"tick_interval_sec": 0.3, "http_port": 0}))


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

    proc = subprocess.Popen(
        [sys.executable, str(LAUNCHER), "daemon", "run"],
        env=_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        started = _wait_for(
            lambda: _events_db(tmp_path).exists() and "daemon.start" in _kinds(_events_db(tmp_path))
        )
        assert started, "daemon never recorded daemon.start"

        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=10)
        assert rc == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    kinds = _kinds(_events_db(tmp_path))
    assert "daemon.stop" in kinds

    lock_body = (tmp_path / "state" / "selly-agent" / "daemon.lock").read_text()
    assert lock_body == ""  # clean shutdown cleared our own holder record


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX signal semantics")
def test_second_instance_exits_zero(tmp_path) -> None:
    (tmp_path / "home").mkdir()
    _write_config(tmp_path)
    env = _env(tmp_path)

    first = subprocess.Popen(
        [sys.executable, str(LAUNCHER), "daemon", "run"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
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
