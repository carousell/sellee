"""logs CLI: duration parsing, formatting, listing/filtering, and a --follow E2E."""

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

from selly_agent import daemon, logs_cli, migrations, paths
from selly_agent.db import Database
from selly_agent.events import Event, EventStore, level_for

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "bin" / "selly-agent"


def _args(**overrides) -> SimpleNamespace:
    base = {
        "follow": False,
        "pass_id": None,
        "since": None,
        "kinds": None,
        "json": False,
        "all": False,
        "web": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_parse_since_units() -> None:
    now = time.time()
    assert logs_cli._parse_since("30s") == pytest.approx(now - 30, abs=2)
    assert logs_cli._parse_since("15m") == pytest.approx(now - 900, abs=2)
    assert logs_cli._parse_since("2h") == pytest.approx(now - 7200, abs=2)
    assert logs_cli._parse_since("1d") == pytest.approx(now - 86400, abs=2)


def test_parse_since_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        logs_cli._parse_since("soon")


def test_format_line_shape() -> None:
    ev = Event(seq=1, ts=0.0, pass_id=None, kind="daemon.start", payload={"pid": 7})
    line = logs_cli._format(ev)
    assert "daemon.start" in line
    assert "pass=-" in line
    assert '{"pid":7}' in line


def test_level_for_classification() -> None:
    assert level_for("task.ok") == "routine"
    assert level_for("task.start") == "routine"
    assert level_for("task.error") == "warn"
    assert level_for("daemon.start") == "info"  # unlisted kinds default to info


def _seed(*kinds: str) -> None:
    paths.ensure_runtime_dirs()
    events_db = Database(paths.events_db())
    migrations.run_startup_migrations(
        data_db=Database(paths.selly_db()),
        events_db=events_db,
        backups_dir=paths.backups_dir(),
        backups_keep=5,
    )
    store = EventStore(events_db)
    for kind in kinds:
        store.record(kind, {"task": "pass_lane"})


def test_default_hides_routine(xdg_tmp, capsys) -> None:
    _seed("task.ok", "daemon.start")
    logs_cli.run(_args())
    out = capsys.readouterr().out
    assert "daemon.start" in out
    assert "task.ok" not in out


def test_all_shows_routine(xdg_tmp, capsys) -> None:
    _seed("task.ok")
    logs_cli.run(_args(all=True))
    assert "task.ok" in capsys.readouterr().out


def test_explicit_kind_overrides_routine_floor(xdg_tmp, capsys) -> None:
    _seed("task.ok")
    logs_cli.run(_args(kinds=["task.ok"]))
    assert "task.ok" in capsys.readouterr().out


def test_format_ndjson_shape() -> None:
    ev = Event(seq=1, ts=0.0, pass_id=None, kind="daemon.start", payload={"pid": 7})
    obj = json.loads(logs_cli._format_ndjson(ev))
    assert next(iter(obj)) == "@ts"  # @ts leads the wire form
    assert obj["level"] == "info"  # derived from kind
    assert obj["seq"] == 1
    assert obj["ts"] == 0.0  # raw epoch retained alongside @ts
    assert obj["pass_id"] is None
    assert obj["kind"] == "daemon.start"
    assert obj["payload"] == {"pid": 7}


def test_json_mode_emits_ndjson(xdg_tmp, capsys) -> None:
    daemon.run_daemon(once=True)
    assert logs_cli.run(_args(json=True)) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines
    parsed = [json.loads(line) for line in lines]  # every line is valid JSON
    assert all(next(iter(obj)) == "@ts" for obj in parsed)
    assert any(obj["kind"] == "daemon.start" for obj in parsed)


def test_no_db_reports_and_returns_zero(xdg_tmp, capsys) -> None:
    assert logs_cli.run(_args()) == 0
    assert "no events yet" in capsys.readouterr().err


def test_lists_events(xdg_tmp, capsys) -> None:
    daemon.run_daemon(once=True)
    assert logs_cli.run(_args()) == 0
    out = capsys.readouterr().out
    assert "daemon.start" in out
    assert "daemon.stop" in out


def test_filters_by_kind(xdg_tmp, capsys) -> None:
    daemon.run_daemon(once=True)
    logs_cli.run(_args(kinds=["daemon.stop"]))
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

    # seed the events DB so the tail has something to open
    subprocess.run([sys.executable, str(LAUNCHER), "daemon", "run", "--once"], env=env, check=True)

    follower = subprocess.Popen(
        [sys.executable, str(LAUNCHER), "logs", "--follow"],
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
        assert seen, "logs --follow never surfaced the live writer's event"
    finally:
        follower.terminate()
        follower.wait(timeout=5)


# --- --web -------------------------------------------------------------------------------------


@pytest.fixture
def opened(monkeypatch):
    """Records what would have been handed to a browser."""
    urls = []
    monkeypatch.setattr(logs_cli.webbrowser, "open", lambda url: urls.append(url) or True)
    return urls


@pytest.fixture
def daemon_up(xdg_tmp, monkeypatch):
    from selly_agent import control, secrets

    paths.ensure_config_dir()
    secrets.write_secret(paths.mcp_token_path(), "ATTENDEDTOKEN")
    monkeypatch.setattr(control, "get", lambda *a, **k: (200, {"bound": False}))


def test_web_prints_the_url_and_opens_it(daemon_up, opened, capsys) -> None:
    assert logs_cli.run(_args(web=True)) == 0
    printed = capsys.readouterr().out.strip()
    assert "token=ATTENDEDTOKEN" in printed
    assert "/tail?" in printed
    # printed first, opened second — the URL is the output even when opening does nothing
    assert opened == [printed]


def test_web_passes_since_through_in_the_pages_own_grammar(daemon_up, opened) -> None:
    assert logs_cli.run(_args(web=True, since="15m")) == 0
    assert "since=15m" in opened[0]


def test_web_rejects_a_malformed_since_before_reaching_the_daemon(
    daemon_up, opened, capsys
) -> None:
    assert logs_cli.run(_args(web=True, since="soon")) == 2
    assert opened == []
    assert "--since must look like" in capsys.readouterr().err


@pytest.mark.parametrize(
    "flag",
    [{"follow": True}, {"pass_id": "p1"}, {"kinds": ["task.ok"]}, {"json": True}, {"all": True}],
)
def test_web_refuses_the_flags_the_page_cannot_honour(daemon_up, opened, capsys, flag) -> None:
    """Silently ignoring a filter would read as though it had applied."""
    assert logs_cli.run(_args(web=True, **flag)) == 2
    assert opened == []
    assert "--web composes only with --since" in capsys.readouterr().err


def test_web_needs_a_running_daemon(xdg_tmp, opened, monkeypatch, capsys) -> None:
    """Unlike the plain tail, the page is served by the daemon — it cannot read the DB directly."""
    from selly_agent import control, secrets

    paths.ensure_config_dir()
    secrets.write_secret(paths.mcp_token_path(), "ATTENDEDTOKEN")

    def unreachable(*a, **k):
        raise control.DaemonUnreachable("connection refused")

    monkeypatch.setattr(control, "get", unreachable)

    assert logs_cli.run(_args(web=True)) == 1
    assert opened == []
    assert "daemon start" in capsys.readouterr().err


def test_the_plain_tail_still_needs_no_daemon(xdg_tmp, capsys) -> None:
    """The read-only-tail property is the reason `logs` works on a stopped install."""
    assert logs_cli.run(_args()) == 0
    assert "no events yet" in capsys.readouterr().err
