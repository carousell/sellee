"""The pass runner: run_pass classification, ledger events, workspace lifecycle, token revoke."""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from sellee import passes, paths, retention
from sellee.config import Config
from sellee.store import ClaimedPass


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
if mode == "stdin":
    # Blocks until the runner closes the handle, the way the real CLI reads a bare -p prompt.
    # Nothing is emitted before the read, so a runner that never closes wedges both sides.
    prompt = sys.stdin.read()
    emit({"type": "system", "subtype": "init", "session_id": "s1", "tools": []})
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": prompt}]}})
    emit({"type": "result", "subtype": "success", "is_error": not prompt,
          "num_turns": 1, "session_id": "s1", "usage": {"input_tokens": 1}})
    sys.exit(0 if prompt else 1)
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


def test_the_prompt_is_delivered_on_stdin_not_argv(bus, store, fake_harness, xdg_tmp) -> None:
    """The prompt is the seller's item data and, for a reply, the buyer conversation, so it stays
    out of a world-readable argv. The runner has to write it and close, in that order and before
    anything waits on the process — a handle left open leaves the harness reading forever.

    Run on a thread with a join timeout: a write that blocked would hang the suite otherwise, and
    the short pass deadline turns a missing close into a plain assertion failure.
    """
    paths.ensure_state_dirs()
    store.enqueue_pass("publish", {"item_id": "item_1"})
    claimed = store.claim_queued_pass()

    delivered = {}

    def argv_builder(spec):
        delivered["prompt"] = spec.prompt
        return [sys.executable, str(fake_harness), "stdin"]

    deps = passes.PassDeps(
        bus=bus,
        store=store,
        config=Config(pass_deadline_sec=20.0),
        auth=FakeAuth(),
        http_endpoint="http://127.0.0.1:1/mcp",
        stop_event=threading.Event(),
        argv_builder=argv_builder,
    )

    out = {}
    runner = threading.Thread(target=lambda: out.update(cls=passes.run_pass(deps, claimed)))
    runner.start()
    runner.join(timeout=60)
    assert not runner.is_alive(), "run_pass never returned — writing the prompt deadlocked"

    assert out["cls"] == "ok"
    # The harness echoes back what it read, so this is the prompt making the whole round trip.
    assert _events(bus, "pass.message")[0].payload["text"] == delivered["prompt"]
    assert delivered["prompt"]


def test_a_large_prompt_does_not_block_the_spawn_path(tmp_path) -> None:
    """A coalesced reply prompt carries every waiting buyer's conversation and can be larger than
    a pipe buffer. Writing it inline would then park the spawn mid-prompt against a harness that
    has not started reading — before the babysitter exists to time the pass out, so nothing would
    ever recover it. Handing over has to return whether or not the child is listening."""
    script = tmp_path / "noread.py"
    script.write_text("import time\ntime.sleep(30)\n")  # alive, never reads stdin
    proc = subprocess.Popen(  # noqa: S603 — our own interpreter and script
        [sys.executable, str(script)], stdin=subprocess.PIPE, text=True
    )
    try:
        started = time.monotonic()
        passes._write_prompt(proc, "x" * (1 << 20))  # comfortably past any pipe buffer
        assert time.monotonic() - started < 5, "handing over the prompt blocked the spawn path"
    finally:
        proc.kill()
        proc.wait(timeout=10)


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
