"""Pause: absolute and immediate. The pass lane claims nothing while paused, a running pass is
killed by the babysitter's pause watch (classified `paused`), send-capable tools refuse, and the
attended pause/resume tools flip the flag. A missing control row reads as not paused.
"""

from __future__ import annotations

import sys
import threading

import pytest

from sellee import passes, paths
from sellee.config import Config
from sellee.tools import TIER_ATTENDED
from sellee.tools.registry import ToolError, dispatch

# A fake harness that emits an init line then sleeps well past any test deadline, so the only way
# it ends is the babysitter killing it.
_SLEEPER = """\
import json, sys, time
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}), flush=True)
time.sleep(60)
"""


class _FakeAuth:
    def mint_pass_token(self, tier, pass_id, expiry_ts, scope=None):
        return f"tok-{pass_id}"

    def revoke_pass_token(self, token):
        pass


def _deps(bus, store, argv):
    return passes.PassDeps(
        bus=bus,
        store=store,
        config=Config(pass_deadline_sec=900.0),
        auth=_FakeAuth(),
        http_endpoint="http://127.0.0.1:1/mcp",
        stop_event=threading.Event(),
        argv_builder=lambda spec: argv,
    )


# --- pass lane gate --------------------------------------------------------------------------


def test_pass_lane_claims_nothing_while_paused(bus, store, xdg_tmp) -> None:
    paths.ensure_state_dirs()
    store.enqueue_pass("publish", {"item_id": "item_1"})
    store.set_paused(True, source="test")
    # argv would fail loudly if a pass were ever spawned — it must not be.
    passes.pass_lane(_deps(bus, store, ["/bin/false"]))
    assert store.count_queued_passes() == 1  # still queued, never claimed
    assert not [e for e in bus.store.read() if e.kind == "pass.start"]


def test_pass_lane_resumes_claiming_after_unpause(bus, store, xdg_tmp) -> None:
    paths.ensure_state_dirs()
    store.enqueue_pass("publish", {"item_id": "item_1"})
    store.set_paused(True, source="test")
    passes.pass_lane(_deps(bus, store, ["/bin/false"]))
    store.set_paused(False, source="test")
    passes.pass_lane(_deps(bus, store, [sys.executable, "-c", "import sys; sys.exit(0)"]))
    assert store.count_queued_passes() == 0  # claimed and run once resumed


# --- babysitter mid-pass kill ---------------------------------------------------------------


def test_running_pass_is_killed_when_paused(bus, store, xdg_tmp, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(passes, "_PAUSE_WATCH_SEC", 0.05)  # tighten the watch for the test
    paths.ensure_state_dirs()
    script = tmp_path / "sleeper.py"
    script.write_text(_SLEEPER)
    pid = store.enqueue_pass("publish", {"item_id": "item_1"})
    claimed = store.claim_queued_pass()
    store.set_paused(True, source="test")  # pause while it runs

    cls = passes.run_pass(_deps(bus, store, [sys.executable, str(script)]), claimed)
    assert cls == "paused"
    assert store.get_pass(pid)["status"] == "error"
    end = [e for e in bus.store.read() if e.kind == "pass.end"][0]
    assert end.payload["class"] == "paused"


# --- tool refusals --------------------------------------------------------------------------


def test_send_reply_refuses_when_paused(make_ctx, store) -> None:
    ctx = make_ctx(TIER_ATTENDED)
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    store.create_thread(
        thread_id="carousell:t1",
        side="sell",
        market="carousell",
        counterpart_handle="b",
        item_id=item["id"],
    )
    store.set_paused(True, source="test")
    result = dispatch("send_reply", {"thread_id": "carousell:t1", "text": "hi"}, ctx)
    assert result["status"] == "paused"


def test_publish_refuses_when_paused(make_ctx, store) -> None:
    ctx = make_ctx(TIER_ATTENDED, rail_factory=lambda: None)
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    store.set_paused(True, source="test")
    with pytest.raises(ToolError, match="paused"):
        dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)


# --- attended control tools -----------------------------------------------------------------


def test_pause_and_resume_tools_flip_the_flag(make_ctx, store) -> None:
    ctx = make_ctx(TIER_ATTENDED)
    assert dispatch("pause_agent", {"source": "session"}, ctx) == {"paused": True}
    assert store.is_paused() is True
    assert dispatch("resume_agent", {}, ctx) == {"paused": False}
    assert store.is_paused() is False
