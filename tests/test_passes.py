"""The pass runner: run_pass classification, ledger events, workspace lifecycle, token revoke."""

from __future__ import annotations

import sys
import threading

import pytest

from selly_agent import passes, paths, retention
from selly_agent.config import Config
from selly_agent.store import ClaimedPass


class FakeAuth:
    def __init__(self):
        self.minted = []
        self.revoked = []

    def mint_pass_token(self, tier, pass_id, expiry_ts, scope=None):
        token = f"tok-{pass_id}"
        self.minted.append((token, tier, pass_id, scope))
        return token

    def revoke_pass_token(self, token):
        self.revoked.append(token)


# A fake harness: emits a couple of stream-json lines then exits with a chosen code. Written to a
# tmp file and invoked as the pass argv, so run_pass exercises real spawn/stream/reap/cleanup.
_FAKE_HARNESS = """\
import json, sys, time
def emit(obj):
    print(json.dumps(obj), flush=True)
mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
if mode == "sleep":
    emit({"type": "system", "subtype": "init", "session_id": "s1", "tools": []})
    time.sleep(60)
emit({"type": "system", "subtype": "init", "session_id": "s1", "tools": ["get_item"]})
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}})
subtype = "error_max_turns" if mode == "cap" else "success"
emit({"type": "result", "subtype": subtype, "is_error": mode != "ok",
      "num_turns": 1, "session_id": "s1", "usage": {"input_tokens": 1}})
# a clean run exits 0; an error and a max-turns termination both exit non-zero, as the real CLI does
sys.exit(0 if mode == "ok" else 1)
"""


@pytest.fixture
def fake_harness(tmp_path):
    script = tmp_path / "fake_harness.py"
    script.write_text(_FAKE_HARNESS)
    return script


def _deps(bus, store, fake_harness, *, mode="ok", deadline=900.0):
    def argv_builder(spec):
        return [sys.executable, str(fake_harness), mode]

    return passes.PassDeps(
        bus=bus,
        store=store,
        config=Config(pass_deadline_sec=deadline),
        auth=FakeAuth(),
        http_endpoint="http://127.0.0.1:1/mcp",
        stop_event=threading.Event(),
        argv_builder=argv_builder,
    )


def _events(bus, kind):
    return [e for e in bus.store.read() if e.kind == kind]


def test_run_pass_ok_ledgers_and_cleans_up(bus, store, fake_harness, xdg_tmp) -> None:
    paths.ensure_state_dirs()
    pid = store.enqueue_pass("publish", {"item_id": "item_1"})
    claimed = store.claim_queued_pass()
    deps = _deps(bus, store, fake_harness, mode="ok")

    cls = passes.run_pass(deps, claimed)
    assert cls == "ok"

    row = store.get_pass(pid)
    assert row["status"] == "done" and row["class"] == "ok" and row["rc"] == 0

    starts = _events(bus, "pass.start")
    ends = _events(bus, "pass.end")
    assert starts and starts[0].pass_id == pid
    assert ends and ends[0].pass_id == pid and ends[0].payload["is_error"] is False
    # stream events were parsed live and correlated by pass_id
    assert _events(bus, "pass.init")[0].pass_id == pid
    assert _events(bus, "pass.message")[0].payload["text"] == "done"

    # token revoked, workspace swept
    assert deps.auth.revoked == [f"tok-{pid}"]
    assert not paths.pass_workspace_dir(pid).exists()


def test_run_pass_error_exit_classifies_error(bus, store, fake_harness, xdg_tmp) -> None:
    paths.ensure_state_dirs()
    pid = store.enqueue_pass("publish", {"item_id": "item_1"})
    claimed = store.claim_queued_pass()
    cls = passes.run_pass(_deps(bus, store, fake_harness, mode="err"), claimed)
    assert cls == "error"
    assert store.get_pass(pid)["status"] == "error"


def test_run_pass_cap_hit_classified(bus, store, fake_harness, xdg_tmp) -> None:
    paths.ensure_state_dirs()
    store.enqueue_pass("publish", {"item_id": "item_1"})
    claimed = store.claim_queued_pass()
    # exit 0 but result subtype error_max_turns -> cap_hit
    cls = passes.run_pass(_deps(bus, store, fake_harness, mode="cap"), claimed)
    assert cls == "cap_hit"


def test_run_pass_missing_item_is_spawn_error(bus, store, fake_harness, xdg_tmp) -> None:
    paths.ensure_state_dirs()
    pid = store.enqueue_pass("publish", {})  # no item_id
    claimed = store.claim_queued_pass()
    cls = passes.run_pass(_deps(bus, store, fake_harness), claimed)
    assert cls == "spawn_error"
    assert store.get_pass(pid)["class"] == "spawn_error"


def test_spawn_error_when_argv_builder_raises(bus, store, xdg_tmp) -> None:
    paths.ensure_state_dirs()
    store.enqueue_pass("publish", {"item_id": "item_1"})
    claimed = store.claim_queued_pass()

    def raising_builder(spec):
        raise passes.SpawnError("claude binary not found")

    deps = passes.PassDeps(
        bus=bus,
        store=store,
        config=Config(),
        auth=FakeAuth(),
        http_endpoint="http://127.0.0.1:1/mcp",
        stop_event=threading.Event(),
        argv_builder=raising_builder,
    )
    assert passes.run_pass(deps, claimed) == "spawn_error"
    end = _events(bus, "pass.end")[0]
    assert end.payload["class"] == "spawn_error" and "not found" in end.payload["error"]


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX process groups")
def test_deadline_kills_pass_and_classifies_timeout(bus, store, fake_harness, xdg_tmp) -> None:
    paths.ensure_state_dirs()
    store.enqueue_pass("publish", {"item_id": "item_1"})
    claimed = store.claim_queued_pass()
    deps = _deps(bus, store, fake_harness, mode="sleep", deadline=0.5)
    cls = passes.run_pass(deps, claimed)
    assert cls == "timeout"


def test_pass_lane_claims_and_runs_one(bus, store, fake_harness, xdg_tmp) -> None:
    paths.ensure_state_dirs()
    pid = store.enqueue_pass("publish", {"item_id": "item_1"})
    passes.pass_lane(_deps(bus, store, fake_harness, mode="ok"))
    assert store.get_pass(pid)["status"] == "done"


def test_pass_lane_fails_stale_running_loudly(
    bus, store, fake_harness, xdg_tmp, monkeypatch
) -> None:
    paths.ensure_state_dirs()
    pid = store.enqueue_pass("publish", {"item_id": "item_1"})
    store.claim_queued_pass()  # -> running now

    # make 'now' far in the future so the running row reads as stale
    future = store.get_pass(pid)["started_ts"] + 10_000
    deps = _deps(bus, store, fake_harness)
    deps.now = lambda: future
    passes.pass_lane(deps)

    assert store.get_pass(pid)["status"] == "error"
    assert store.get_pass(pid)["class"] == "stale"
    stale_ends = [e for e in _events(bus, "pass.end") if e.payload.get("class") == "stale"]
    assert stale_ends and stale_ends[0].pass_id == pid


def test_pass_end_survives_retention_prune(bus, store, fake_harness, xdg_tmp) -> None:
    paths.ensure_state_dirs()
    claimed = ClaimedPass(
        pass_id=store.enqueue_pass("publish", {"item_id": "i"}),
        type="publish",
        payload={"item_id": "i"},
    )
    store.claim_queued_pass()
    passes.run_pass(_deps(bus, store, fake_harness, mode="ok"), claimed)

    # prune everything older than 'now' (retention_days back from a far-future now)
    retention.run_retention(
        bus=bus,
        retention_days=1,
        routine_events_retention_hours=24,
        backups_dir=paths.backups_dir(),
        backups_keep=5,
        logs_dir=paths.logs_dir(),
        now=1e18,
    )
    kinds = [e.kind for e in bus.store.read()]
    assert "pass.end" in kinds  # a kept summary survived
    assert "pass.init" not in kinds  # a verbose per-line event was pruned
