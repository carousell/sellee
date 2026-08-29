"""The pacing engine + the store-backed reserve + the publish reservation.

Covers the ported semantics: record-at-reserve, verdicts go/wait/quiet, quiet checked before cap
before jitter, publish jitter-free, FAST-mode, and concurrency (N reservers, cap C → exactly C go).
`now` is an engine parameter so the wall-clock-hour logic is deterministic in tests.
"""

from __future__ import annotations

import concurrent.futures
from datetime import datetime

import pytest

from sellee.config import Config
from sellee.engines import pacing
from sellee.tools.registry import TIER_PASS_PUBLISH, ToolError, dispatch


def _noon_on(day="2026-07-22") -> float:
    return datetime.fromisoformat(f"{day}T12:00:00").timestamp()


def _midnight_ish(day="2026-07-22") -> float:
    return datetime.fromisoformat(f"{day}T02:00:00").timestamp()  # inside default quiet 23..8


# --- engine ------------------------------------------------------------------------------------


def test_go_records_and_jitters_within_range() -> None:
    cfg = pacing.resolve(Config(reply_delay_sec=[1, 3]), quiet_hours=[0, 0])
    r = pacing.evaluate([], now=_noon_on(), cfg=cfg, kind="reply")
    assert r["verdict"] == "go" and r["record"] is True
    assert 1.0 <= r["delay_sec"] <= 3.0


def test_publish_is_jitter_free() -> None:
    cfg = pacing.resolve(Config(reply_delay_sec=[1, 3]), quiet_hours=[0, 0])
    r = pacing.evaluate([], now=_noon_on(), cfg=cfg, kind="publish")
    assert r["verdict"] == "go" and r["delay_sec"] == 0.0


def test_at_cap_waits_without_recording() -> None:
    cfg = pacing.resolve(Config(max_actions_per_hour=3), quiet_hours=[0, 0])
    now = _noon_on()
    r = pacing.evaluate([now - 10, now - 20, now - 30], now=now, cfg=cfg, kind="reply")
    assert r["verdict"] == "wait" and r["record"] is False and r["delay_sec"] > 0


def test_quiet_checked_before_cap_and_no_record() -> None:
    # default quiet hours 23..8; at 02:00 even an empty ledger is quiet, never a go. A followup is
    # outreach we *start*, which is exactly what the window holds.
    cfg = pacing.resolve(Config(max_actions_per_hour=12), quiet_hours=[1380, 480])
    r = pacing.evaluate([], now=_midnight_ish(), cfg=cfg, kind="followup")
    assert r["verdict"] == "quiet" and r["record"] is False


@pytest.mark.parametrize("kind", pacing.REACTIVE_KINDS)
def test_reactive_kinds_answer_during_quiet_hours(kind) -> None:
    # A buyer who writes at 2am is awake and waiting. Answering them is not the pattern the quiet
    # window exists to hide — starting a conversation at 2am is.
    cfg = pacing.resolve(Config(max_actions_per_hour=12), quiet_hours=[1380, 480])
    r = pacing.evaluate([], now=_midnight_ish(), cfg=cfg, kind=kind)
    assert r["verdict"] == "go" and r["record"] is True


@pytest.mark.parametrize("kind", ("followup", "nudge", "publish"))
def test_proactive_kinds_stay_held_during_quiet_hours(kind) -> None:
    cfg = pacing.resolve(Config(max_actions_per_hour=12), quiet_hours=[1380, 480])
    r = pacing.evaluate([], now=_midnight_ish(), cfg=cfg, kind=kind)
    assert r["verdict"] == "quiet" and r["record"] is False


def test_cap_still_bites_a_reactive_kind_during_quiet_hours() -> None:
    # Only the quiet gate is loosened: account safety's hard cap still applies at every hour, so an
    # exempt reply can never outrun it.
    cfg = pacing.resolve(Config(max_actions_per_hour=2), quiet_hours=[1380, 480])
    now = _midnight_ish()
    r = pacing.evaluate([now - 10, now - 20], now=now, cfg=cfg, kind="reply")
    assert r["verdict"] == "wait" and r["record"] is False


def test_fast_mode_zeroes_jitter_lifts_cap_disables_quiet() -> None:
    cfg = pacing.resolve(
        Config(pacing_mode="fast", max_actions_per_hour=1, reply_delay_sec=[1, 3]),
        quiet_hours=[1380, 480],
    )
    assert cfg.cap == 60
    # even at 02:00 and with a full small cap's worth of history, fast still goes, jitter-free
    now = _midnight_ish()
    r = pacing.evaluate([now - 1] * 5, now=now, cfg=cfg, kind="reply")
    assert r["verdict"] == "go" and r["delay_sec"] == 0.0


def test_interactive_selects_its_range() -> None:
    cfg = pacing.resolve(
        Config(reply_delay_sec=[3, 3], interactive_reply_delay_sec=[1, 1]), quiet_hours=[0, 0]
    )
    interactive = pacing.evaluate([], now=_noon_on(), cfg=cfg, kind="reply", interactive=True)
    unattended = pacing.evaluate([], now=_noon_on(), cfg=cfg, kind="reply", interactive=False)
    assert interactive["delay_sec"] == 1.0
    assert unattended["delay_sec"] == 3.0


# --- store-backed reserve ----------------------------------------------------------------------


def test_reserve_records_on_go_and_compacts(store) -> None:
    cfg = pacing.resolve(Config(max_actions_per_hour=5, reply_delay_sec=[0, 0]), quiet_hours=[0, 0])
    now = _noon_on()
    # an ancient action is compacted away on the next go
    store.reserve_action(marketplace="fb", kind="reply", cfg=cfg, now=now - 10000)
    r = store.reserve_action(marketplace="fb", kind="reply", cfg=cfg, now=now)
    assert r["verdict"] == "go"
    rows = store._db.query("SELECT ts FROM pacing_actions WHERE marketplace='fb'")
    assert len(rows) == 1  # the stale row was pruned, only the fresh reserve remains


def test_reserve_wait_records_nothing(store) -> None:
    cfg = pacing.resolve(Config(max_actions_per_hour=1, reply_delay_sec=[0, 0]), quiet_hours=[0, 0])
    now = _noon_on()
    assert store.reserve_action(marketplace="fb", kind="reply", cfg=cfg, now=now)["verdict"] == "go"
    blocked = store.reserve_action(marketplace="fb", kind="reply", cfg=cfg, now=now)
    assert blocked["verdict"] == "wait"
    rows = store._db.query("SELECT ts FROM pacing_actions WHERE marketplace='fb'")
    assert len(rows) == 1  # the blocked reserve added nothing


def test_cap_shared_across_sell_and_buy(store) -> None:
    cfg = pacing.resolve(Config(max_actions_per_hour=1, reply_delay_sec=[0, 0]), quiet_hours=[0, 0])
    now = _noon_on()
    store.reserve_action(marketplace="fb", kind="reply", cfg=cfg, now=now)  # sell-side
    buy = store.reserve_action(marketplace="fb", kind="liaison", cfg=cfg, now=now)  # buy-side
    assert buy["verdict"] == "wait"  # one marketplace ledger, both sides


def test_concurrent_reservers_never_exceed_cap(store) -> None:
    cfg = pacing.resolve(Config(max_actions_per_hour=3, reply_delay_sec=[0, 0]), quiet_hours=[0, 0])
    now = _noon_on()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = [
            f.result()
            for f in [
                ex.submit(store.reserve_action, marketplace="fb", kind="reply", cfg=cfg, now=now)
                for _ in range(20)
            ]
        ]
    gos = [r for r in results if r["verdict"] == "go"]
    assert len(gos) == 3  # exactly the cap, never more — the transaction serializes reservers


def test_peek_answers_the_verdict_without_taking_the_slot(store) -> None:
    """For a caller deciding whether to start work a refusal would waste. Two peeks in a row must
    answer the same way — a peek that recorded would be a reservation with the wrong name."""
    cfg = pacing.resolve(Config(max_actions_per_hour=1, reply_delay_sec=[0, 0]), quiet_hours=[0, 0])
    now = _noon_on()

    first = store.peek_action(marketplace="fb", kind="publish", cfg=cfg, now=now)
    assert (first["verdict"], first["record"]) == ("go", False)
    assert store.peek_action(marketplace="fb", kind="publish", cfg=cfg, now=now)["verdict"] == "go"
    assert not store._db.query("SELECT ts FROM pacing_actions WHERE marketplace='fb'")

    store.reserve_action(marketplace="fb", kind="publish", cfg=cfg, now=now)
    assert (
        store.peek_action(marketplace="fb", kind="publish", cfg=cfg, now=now)["verdict"] == "wait"
    )


def test_peek_reports_quiet_hours(store) -> None:
    cfg = pacing.resolve(Config(reply_delay_sec=[0, 0]), quiet_hours=[2300, 800])
    peeked = store.peek_action(marketplace="fb", kind="publish", cfg=cfg, now=_midnight_ish())
    assert peeked["verdict"] == "quiet"


# --- publish reservation -----------------------------------------------------------------------


class _FakeRail:
    def create_listing(self, args):
        return {"listing_id": "L1", "url": "https://www.carousell.ai/listing/1"}

    def verify_listing_url(self, url):
        return None


def test_publish_reserves_and_caps(make_ctx, store) -> None:
    cfg_obj = Config(max_actions_per_hour=1)
    ctx = make_ctx(TIER_PASS_PUBLISH, rail_factory=lambda: _FakeRail(), config=cfg_obj)
    first = store.create_item(title="A", list_price=80.0, currency="SGD")
    second = store.create_item(title="B", list_price=90.0, currency="SGD")
    dispatch("carousell_ai_publish_listing", {"item_id": first["id"]}, ctx)
    with pytest.raises(ToolError, match="paced"):
        dispatch("carousell_ai_publish_listing", {"item_id": second["id"]}, ctx)
