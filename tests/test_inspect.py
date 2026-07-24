"""inspect CLI: duration parsing, formatting, listing/filtering, and a --follow E2E."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from selly_agent import daemon, inspect_cli
from selly_agent.events import Event

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "bin" / "selly-agent"


def _args(**overrides) -> SimpleNamespace:
    base = {"follow": False, "pass_id": None, "since": None, "kinds": None, "json": False}
    base.update(overrides)
    return SimpleNamespace(**base)


def test_parse_since_units() -> None:
    now = time.time()
    assert inspect_cli._parse_since("30s") == pytest.approx(now - 30, abs=2)
    assert inspect_cli._parse_since("15m") == pytest.approx(now - 900, abs=2)
    assert inspect_cli._parse_since("2h") == pytest.approx(now - 7200, abs=2)
    assert inspect_cli._parse_since("1d") == pytest.approx(now - 86400, abs=2)


def test_parse_since_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        inspect_cli._parse_since("soon")


def test_format_line_shape() -> None:
    ev = Event(seq=1, ts=0.0, pass_id=None, kind="daemon.start", payload={"pid": 7})
    line = inspect_cli._format(ev)
    assert "daemon.start" in line
    assert "pass=-" in line
    assert '{"pid":7}' in line


def test_format_ndjson_shape() -> None:
    ev = Event(seq=1, ts=0.0, pass_id=None, kind="daemon.start", payload={"pid": 7})
    obj = json.loads(inspect_cli._format_ndjson(ev))
    assert next(iter(obj)) == "@ts"  # @ts leads the wire form
    assert obj["seq"] == 1
    assert obj["ts"] == 0.0  # raw epoch retained alongside @ts
    assert obj["pass_id"] is None
    assert obj["kind"] == "daemon.start"
    assert obj["payload"] == {"pid": 7}


def test_json_mode_emits_ndjson(xdg_tmp, capsys) -> None:
    daemon.run_daemon(once=True)
    assert inspect_cli.run(_args(json=True)) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines
    parsed = [json.loads(line) for line in lines]  # every line is valid JSON
    assert all(next(iter(obj)) == "@ts" for obj in parsed)
    assert any(obj["kind"] == "daemon.start" for obj in parsed)


def test_no_db_reports_and_returns_zero(xdg_tmp, capsys) -> None:
    assert inspect_cli.run(_args()) == 0
    assert "no events yet" in capsys.readouterr().err


def test_lists_events(xdg_tmp, capsys) -> None:
    daemon.run_daemon(once=True)
    assert inspect_cli.run(_args()) == 0
    out = capsys.readouterr().out
    assert "daemon.start" in out
    assert "daemon.stop" in out


def test_filters_by_kind(xdg_tmp, capsys) -> None:
    daemon.run_daemon(once=True)
    inspect_cli.run(_args(kinds=["daemon.stop"]))
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines
    assert all("daemon.stop" in line for line in lines)


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="subprocess/signal semantics")
def test_follow_sees_live_writer(tmp_path) -> None:
    env = dict(os.environ)
    env.update(
        HOME=str(tmp_path / "home"),
        XDG_DATA_HOME=str(tmp_path / "data"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        PYTHONPATH=str(REPO_ROOT / "src"),
    )
    (tmp_path / "home").mkdir()

    # seed the events DB so inspect has something to open
    subprocess.run([sys.executable, str(LAUNCHER), "daemon", "run", "--once"], env=env, check=True)

    follower = subprocess.Popen(
        [sys.executable, str(LAUNCHER), "inspect", "--follow"],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    lines: queue.Queue = queue.Queue()
    threading.Thread(
        target=lambda: [lines.put(line) for line in follower.stdout], daemon=True
    ).start()

    writer = (
        "from selly_agent import paths;"
        "from selly_agent.db import Database;"
        "from selly_agent.events import EventStore;"
        "EventStore(Database(paths.events_db())).record('e2e.marker', {'hi': 1})"
    )
    try:
        # let the follower reach its poll loop, then a separate live writer appends an event
        time.sleep(1.5)
        subprocess.run([sys.executable, "-c", writer], env=env, check=True)

        seen = False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                continue
            if "e2e.marker" in line:
                seen = True
                break
        assert seen, "inspect --follow never surfaced the live writer's event"
    finally:
        follower.terminate()
        follower.wait(timeout=5)
