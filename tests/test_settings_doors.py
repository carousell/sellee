"""The settings doors: fast-path recognition (buttons + exact text tokens), the deterministic
decision router (approve/cancel/undo, double-tap idempotence, stale ids), the quiet-hours drain
hold with urgent bypass, doors while paused, and one button-tap round trip through the real poller
against the fake Bot API.
"""

from __future__ import annotations

from datetime import datetime

from fake_telegram_api import CHAT_ID, FAKE_TOKEN, FakeTelegramAPI
from selly_agent import secrets, settings
from selly_agent.channel import fastpaths, outbound
from selly_agent.channel.telegram.poller import Poller


def _bind(store):
    secrets.write_telegram_bot_token(FAKE_TOKEN)
    store.arm_bind("selly_test_bot", "n1")
    store.complete_bind(CHAT_ID, update_offset=1)


def _propose(store, value=None):
    value = [23, 9] if value is None else value
    cid = store.new_change_id()
    store.propose_setting_change(
        "quiet_hours",
        value,
        change_id=cid,
        prior_value=settings.get(store, "quiet_hours"),
        notice_text="Approve?",
        notice_controls=[["Approve", f"{cid}:{settings.CB_APPROVE}"]],
    )
    return cid


# --- fast-path recognition --------------------------------------------------------------------


def test_recognizes_button_and_text_token() -> None:
    button = {"kind": "action", "payload": {"choice": settings.CB_APPROVE, "ref": "chg_abc"}}
    token = {"kind": "text", "text": "approve chg_abc", "payload": {}}
    assert fastpaths.is_settings_door(button) and fastpaths.is_fast_path(button)
    assert fastpaths.is_settings_door(token) and fastpaths.is_fast_path(token)


def test_conversational_text_is_not_a_door() -> None:
    # only an exact "<verb> <chg_id>" trips the door — never prose that starts with "approve"
    assert not fastpaths.is_settings_door({"kind": "text", "text": "approve the buyer's offer"})
    assert not fastpaths.is_settings_door({"kind": "text", "text": "undo"})
    assert not fastpaths.is_settings_door({"kind": "text", "text": "approve notachg"})


# --- decision router --------------------------------------------------------------------------


def test_button_approve_applies(store, bus) -> None:
    cid = _propose(store)
    event = {"kind": "action", "payload": {"choice": settings.CB_APPROVE, "ref": cid}}
    reply = fastpaths.handle_settings_door(store, bus, event)
    assert "Applied" in reply
    assert settings.get(store, "quiet_hours") == [23, 9]


def test_text_token_approve_applies(store, bus) -> None:
    cid = _propose(store)
    event = {"kind": "text", "text": f"approve {cid}", "payload": {}}
    fastpaths.handle_settings_door(store, bus, event)
    assert settings.get(store, "quiet_hours") == [23, 9]
    assert store.get_pending_change(cid)["decided_via"] == "token"


def test_cancel_leaves_unchanged(store, bus) -> None:
    cid = _propose(store)
    settings.decide(
        store, bus, change_id=cid, decision=settings.DECIDE_CANCEL, decided_via="button"
    )
    assert store.get_setting("quiet_hours") == [0, 0]  # the seeded value, untouched
    assert store.get_pending_change(cid)["status"] == "cancelled"


def test_undo_round_trip(store, bus) -> None:
    cid = _propose(store)
    settings.decide(
        store, bus, change_id=cid, decision=settings.DECIDE_APPROVE, decided_via="button"
    )
    assert settings.get(store, "quiet_hours") == [23, 9]
    out = settings.decide(
        store, bus, change_id=cid, decision=settings.DECIDE_UNDO, decided_via="button"
    )
    assert out["status"] == "undone"
    assert settings.get(store, "quiet_hours") == [0, 0]


def test_double_tap_is_idempotent(store, bus) -> None:
    cid = _propose(store)
    first = settings.decide(store, bus, change_id=cid, decision="approve", decided_via="button")
    second = settings.decide(store, bus, change_id=cid, decision="approve", decided_via="button")
    assert first["status"] == "applied"
    assert second["status"] == "not_pending" and "already applied" in second["message"]


def test_stale_id_gets_a_deterministic_answer(store, bus) -> None:
    out = settings.decide(
        store, bus, change_id="chg_ghost", decision="approve", decided_via="token"
    )
    assert out["status"] == "unknown" and "wasn't found" in out["message"]


def test_expired_proposal_is_answered(store, bus) -> None:
    import time

    cid = _propose(store)
    store.expire_pending_changes(cutoff_ts=time.time() + 10)
    out = settings.decide(store, bus, change_id=cid, decision="approve", decided_via="button")
    assert out["status"] in ("not_pending", "expired") and "expired" in out["message"]


def test_doors_work_while_paused(store, bus) -> None:
    store.set_paused(True, source="test")
    cid = _propose(store)
    out = settings.decide(store, bus, change_id=cid, decision="approve", decided_via="cli")
    assert out["status"] == "applied"  # a door is seller-initiated control — it bypasses the pause
    assert settings.get(store, "quiet_hours") == [23, 9]


# --- quiet-hours drain hold -------------------------------------------------------------------


def test_drain_holds_routine_passes_urgent_then_delivers_at_window_end(store, bus, xdg_tmp) -> None:
    from tests.conftest import seed_setting

    _bind(store)
    seed_setting(store, "quiet_hours", [8, 20])  # a window that covers noon
    store.queue_notice("routine update")
    store.queue_notice("URGENT: accept $70?", urgent=True)
    sent: list = []

    def deliver(chat_id, text, controls=None):
        sent.append(text)

    noon = datetime.fromisoformat("2026-07-22T12:00:00").timestamp()
    outbound.drain_notices(store=store, bus=bus, deliver=deliver, now=noon)
    assert sent == ["URGENT: accept $70?"]  # routine held inside the window
    assert store.count_queued_notices() == 1

    evening = datetime.fromisoformat("2026-07-22T21:00:00").timestamp()
    outbound.drain_notices(store=store, bus=bus, deliver=deliver, now=evening)
    assert sent == ["URGENT: accept $70?", "routine update"]  # drains once the window ends
    assert store.count_queued_notices() == 0


# --- catchup surfaces a pending proposal (with its id) ----------------------------------------


def test_catchup_render_lists_pending_change_with_id(store) -> None:
    cid = _propose(store)
    text = fastpaths.render_catchup(store)
    assert "awaiting your OK" in text
    assert cid in text and "→" in text  # the id the seller needs, and the current→proposed render


# --- one button tap through the real poller ---------------------------------------------------


def test_button_tap_through_poller_applies_and_acks(store, bus, xdg_tmp) -> None:
    _bind(store)
    cid = _propose(store)
    with FakeTelegramAPI() as api:
        from selly_agent.config import Config

        cfg = Config(telegram_api_base=api.base_url)
        api.inject_tap(f"{cid}:{settings.CB_APPROVE}")
        poller = Poller(store=store, config=cfg, bus=bus, stop_event=_never_set(), poll_timeout=0)
        poller.tick()
        assert settings.get(store, "quiet_hours") == [23, 9]  # the tap applied the change
        assert "cbq1" in api.answered  # the callback spinner was answered
        assert any("Applied" in m["text"] for m in api.outbox)  # the door replied


class _never_set:
    def is_set(self):
        return False

    def wait(self, *a):
        return False
