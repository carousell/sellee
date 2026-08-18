"""The settings change ledger in the store: propose (HOLD) → approve/cancel, immediate apply
(ALLOW), single-level undo, supersede-by-key, expiry, and apply-transaction atomicity."""

from __future__ import annotations

import time

import pytest

from sellee import settings as settings_mod
from sellee import store as store_mod


def _propose(store, key="quiet_hours", value=None, prior=None):
    value = [2300, 930] if value is None else value
    prior = settings_mod.get(store, key) if prior is None else prior
    cid = store.new_change_id()
    store.propose_setting_change(
        key,
        value,
        change_id=cid,
        prior_value=prior,
        notice_text="Approve?",
        notice_controls=[["Approve", f"{cid}:setapprove"]],
    )
    return cid


# --- HOLD: propose then approve ---------------------------------------------------------------


def test_propose_writes_pending_and_notice(fresh_store) -> None:
    cid = _propose(fresh_store)
    pending = fresh_store.list_pending_changes()
    assert [p["change_id"] for p in pending] == [cid]
    assert pending[0]["value"] == [2300, 930] and pending[0]["prior_value"] == [2300, 800]
    # the approval notice is queued with its keyboard, durable before any delivery
    notices = fresh_store.list_queued_notices()
    assert len(notices) == 1 and notices[0]["controls"] == [["Approve", f"{cid}:setapprove"]]
    # nothing applied yet
    assert fresh_store.get_setting("quiet_hours") is None


def test_approve_applies_and_echoes(fresh_store) -> None:
    cid = _propose(fresh_store)
    result = fresh_store.approve_setting_change(
        cid,
        decided_via="button",
        notice_text="done",
        notice_controls=[["Undo", f"{cid}:setundo"]],
    )
    assert result["status"] == "applied" and result["value"] == [2300, 930]
    assert settings_mod.get(fresh_store, "quiet_hours") == [2300, 930]
    assert fresh_store.get_pending_change(cid)["status"] == "applied"
    # both the (delivered-nothing-yet) approval notice and the echo are queued
    assert len(fresh_store.list_queued_notices()) == 2


def test_double_approve_is_not_pending(fresh_store) -> None:
    cid = _propose(fresh_store)
    fresh_store.approve_setting_change(cid, decided_via="button", notice_text="done")
    again = fresh_store.approve_setting_change(cid, decided_via="button", notice_text="done again")
    assert again["status"] == "not_pending" and again["current"] == "applied"


def test_cancel_leaves_setting_untouched(fresh_store) -> None:
    cid = _propose(fresh_store)
    result = fresh_store.cancel_setting_change(cid, decided_via="button")
    assert result["status"] == "cancelled"
    assert fresh_store.get_setting("quiet_hours") is None
    assert fresh_store.get_pending_change(cid)["status"] == "cancelled"


# --- ALLOW: immediate apply -------------------------------------------------------------------


def test_apply_now_applies_in_one_shot(fresh_store) -> None:
    cid = fresh_store.new_change_id()
    out = fresh_store.apply_setting_now(
        "quiet_hours",
        [22, 7],
        change_id=cid,
        prior_value=[2300, 800],
        notice_text="set",
    )
    assert out["value"] == [22, 7]
    assert settings_mod.get(fresh_store, "quiet_hours") == [22, 7]
    assert fresh_store.get_pending_change(cid)["status"] == "applied"
    assert fresh_store.get_pending_change(cid)["decided_via"] == "auto"


# --- single-level undo ------------------------------------------------------------------------


def test_undo_restores_prior(fresh_store) -> None:
    cid = _propose(fresh_store)
    fresh_store.approve_setting_change(cid, decided_via="button", notice_text="done")
    undo = fresh_store.undo_setting_change(cid, decided_via="button", notice_text="reverted")
    assert undo["status"] == "undone" and undo["value"] == [2300, 800]
    assert settings_mod.get(fresh_store, "quiet_hours") == [2300, 800]


def test_undo_is_stale_after_a_newer_change(fresh_store) -> None:
    cid = _propose(fresh_store)
    fresh_store.approve_setting_change(cid, decided_via="button", notice_text="done")
    # a newer change moves the same key
    cid2 = fresh_store.new_change_id()
    fresh_store.apply_setting_now(
        "quiet_hours", [1, 2], change_id=cid2, prior_value=[2300, 930], notice_text="again"
    )
    stale = fresh_store.undo_setting_change(cid, decided_via="button", notice_text="reverted")
    assert stale["status"] == "not_undoable" and stale["reason"] == "superseded"


def test_undo_of_unapplied_is_refused(fresh_store) -> None:
    cid = _propose(fresh_store)  # still pending, never applied
    out = fresh_store.undo_setting_change(cid, decided_via="button", notice_text="x")
    assert out["status"] == "not_undoable" and out["reason"] == "not_applied"


# --- supersede-by-key -------------------------------------------------------------------------


def test_new_proposal_supersedes_the_live_one(fresh_store) -> None:
    first = _propose(fresh_store, value=[2300, 930])
    second = _propose(fresh_store, value=[22, 8])
    assert fresh_store.get_pending_change(first)["status"] == "superseded"
    assert [p["change_id"] for p in fresh_store.list_pending_changes()] == [second]


def test_approving_a_superseded_change_is_not_pending(fresh_store) -> None:
    first = _propose(fresh_store, value=[2300, 930])
    _propose(fresh_store, value=[22, 8])
    out = fresh_store.approve_setting_change(first, decided_via="token", notice_text="done")
    assert out["status"] == "not_pending" and out["current"] == "superseded"


# --- expiry -----------------------------------------------------------------------------------


def test_expire_sweep_marks_stale_pending(fresh_store) -> None:
    cid = _propose(fresh_store)
    expired = fresh_store.expire_pending_changes(cutoff_ts=time.time() + 10)
    assert [e["change_id"] for e in expired] == [cid]
    assert fresh_store.get_pending_change(cid)["status"] == "expired"
    assert fresh_store.list_pending_changes() == []


def test_expire_sweep_leaves_fresh_pending(fresh_store) -> None:
    cid = _propose(fresh_store)
    assert fresh_store.expire_pending_changes(cutoff_ts=time.time() - 10) == []
    assert fresh_store.get_pending_change(cid)["status"] == "pending"


# --- apply-transaction atomicity --------------------------------------------------------------


def test_apply_is_all_or_nothing(fresh_store, monkeypatch) -> None:
    # Force the notice insert (the last step of the apply transaction) to fail; the setting upsert
    # and the pending row must roll back with it — never a half-applied change.
    cid = _propose(fresh_store)

    def boom(*args, **kwargs):
        raise RuntimeError("insert blew up")

    monkeypatch.setattr(store_mod, "_insert_notice", boom)
    with pytest.raises(RuntimeError):
        fresh_store.approve_setting_change(cid, decided_via="button", notice_text="done")
    # nothing changed: setting still unset, proposal still pending
    assert fresh_store.get_setting("quiet_hours") is None
    assert fresh_store.get_pending_change(cid)["status"] == "pending"
