"""daemon run --once end to end, and the duplicate-instance exit-0 path (INV-27)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from sellee import config, daemon, heartbeat, lock, paths
from sellee.browser import client as browser_client
from sellee.db import connect_reader
from sellee.events import query_events


def _event_kinds(events_db_path) -> list[str]:
    conn = connect_reader(events_db_path)
    try:
        return [e.kind for e in query_events(conn)]
    finally:
        conn.close()


def _tasks_run(events_db_path) -> set:
    conn = connect_reader(events_db_path)
    try:
        return {e.payload.get("task") for e in query_events(conn) if e.kind == "task.start"}
    finally:
        conn.close()


def _write_config(obj) -> None:
    paths.ensure_config_dir()
    paths.config_path().write_text(json.dumps(obj))


def test_run_once_migrates_heartbeats_and_ledgers(xdg_tmp) -> None:
    _write_config({"http_port": 0})  # an ephemeral port so the test never collides on 7355
    assert config.load().http_port == 0
    rc = daemon.run_daemon(once=True)
    assert rc == 0

    # heartbeat written
    hb = heartbeat.read(paths.heartbeat_path())
    assert hb["pid"] == os.getpid()

    # both DBs migrated
    conn = connect_reader(paths.sellee_db())
    try:
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] >= 1
    finally:
        conn.close()

    kinds = _event_kinds(paths.events_db())
    assert "daemon.start" in kinds
    assert "daemon.stop" in kinds
    assert "migration.applied" in kinds
    assert "task.start" in kinds and "task.ok" in kinds  # retention lane exercised

    # Every lane is registered and survives a tick against an empty store. A lane that is built but
    # never scheduled is the failure this pins: the browser publish shipped that way.
    ran = _tasks_run(paths.events_db())
    assert {"pass_lane", "inbox_read", "reply_lane", "crosslist_lane"} <= ran
    # The release check is deliberately absent: every task's first tick is immediate, so having it
    # here would reach the release host from a smoke run — and from this test.
    assert "update_probe" not in ran
    assert "task.error" not in kinds


def test_duplicate_instance_exits_zero_without_starting(xdg_tmp) -> None:
    paths.ensure_state_dirs()
    held = lock.acquire(paths.lock_path())  # simulate a live holder
    try:
        rc = daemon.run_daemon(once=True)
        assert rc == 0
        # it exited before opening the event store, so no events DB was created
        assert not paths.events_db().exists()
    finally:
        os.close(held.fd)


# --- the browser server's cache ------------------------------------------------------------------


def _record_warm(monkeypatch, *, returncode: int = 0, raises=None):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, "", "boom")

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    return calls


def test_the_daemon_warms_the_pinned_browser_server_at_startup(monkeypatch) -> None:
    # An update that bumps the pin never re-runs setup's warm, and npm may prune a cache we do not
    # own — either way the next spawn is a download that can outrun the client's startup timeout.
    calls = _record_warm(monkeypatch)

    thread = daemon.warm_browser_server(config.Config(), once=False)
    assert thread is not None
    thread.join(timeout=5)

    argv, kwargs = calls[0]
    assert argv == ["npx", "--yes", browser_client.PINNED_MCP_SPEC, "--version"]
    assert kwargs["timeout"] == daemon._BROWSER_WARM_TIMEOUT_SEC


def test_a_failed_warm_is_only_logged(monkeypatch, caplog) -> None:
    _record_warm(monkeypatch, returncode=1)
    with caplog.at_level("WARNING"):
        daemon.warm_browser_server(config.Config(), once=False).join(timeout=5)
    assert "could not warm" in caplog.text

    _record_warm(monkeypatch, raises=OSError("npx is gone"))
    with caplog.at_level("WARNING"):
        daemon.warm_browser_server(config.Config(), once=False).join(timeout=5)
    assert "npx is gone" in caplog.text


def test_no_warm_under_an_override_or_in_a_single_tick_run(monkeypatch) -> None:
    # A foreign command's warming semantics are not ours to guess, and --once is a smoke check that
    # must not reach the network.
    calls = _record_warm(monkeypatch)

    assert daemon.warm_browser_server(config.Config(), once=True) is None
    override = config.Config(playwright_mcp_cmd=["node", "/opt/mcp/cli.js"])
    assert daemon.warm_browser_server(override, once=False) is None
    assert calls == []


# --- the bind address -------------------------------------------------------------------------


def test_only_a_loopback_bind_reads_as_staying_on_this_machine() -> None:
    """The Host header is the client's to choose, so it cannot decide who reaches this port — the
    bind address is the only thing that does."""
    assert daemon._is_loopback_bind("127.0.0.1")
    assert daemon._is_loopback_bind("127.0.0.53")  # all of 127/8, not just the one spelling
    assert daemon._is_loopback_bind("::1")
    assert daemon._is_loopback_bind("localhost")
    assert not daemon._is_loopback_bind("0.0.0.0")  # noqa: S104 — the point of the assertion
    assert not daemon._is_loopback_bind("::")
    assert not daemon._is_loopback_bind("192.168.1.5")
    assert not daemon._is_loopback_bind("*")
    assert not daemon._is_loopback_bind("")


def test_no_comment_claims_the_host_guard_makes_a_wide_bind_safe() -> None:
    """It said so in two places, and it was not true. A reader who believes it concludes the
    wildcard the image sets is harmless anywhere, which is how `docker run --network host`
    becomes a supported-looking way to publish the control surface."""
    root = Path(__file__).resolve().parents[1]
    for path in ("src/sellee/daemon.py", "src/sellee/http_server.py"):
        text = (root / path).read_text()
        assert "SELLEE_BIND_HOST" in text  # reading the file we think we are
        assert "Host guard still only answers to localhost" not in text
        assert "have to arrive addressed to localhost" not in text


def test_a_non_loopback_bind_says_what_it_exposed(caplog) -> None:
    """Silence here is the whole problem: the seller sets a documented variable, or runs the image
    with host networking, and nothing tells them the control surface left the machine."""
    with caplog.at_level("WARNING"):
        daemon._warn_if_exposed("0.0.0.0")  # noqa: S104 — the case under test
    assert "not loopback" in caplog.text
    assert "0.0.0.0" in caplog.text
    assert "security risk" in caplog.text


def test_a_loopback_bind_says_nothing(caplog) -> None:
    with caplog.at_level("WARNING"):
        daemon._warn_if_exposed("127.0.0.1")
    assert caplog.text == ""
