"""The scam engine + its tools: scoring, the merged registry+bank view, the bank state machine,
and the record/retract/scan tools over the store."""

from __future__ import annotations

import pytest

from sellee.engines import hosts, scam
from sellee.store import StoreError
from sellee.tools.registry import ToolError, dispatch

BASE = "https://api.carousell.ai/checkout"


def _allowlist():
    from sellee import marketplaces

    return hosts.build_allowlist(marketplaces.all_marketplaces())


# --- engine ------------------------------------------------------------------------------------


def test_combo_forces_scam() -> None:
    r = scam.scan(
        "I'll arrange the courier, click https://fastpay-sg.top/claim to receive the money",
        allowlist=_allowlist(),
        signatures=[],
        checkout_base=BASE,
    )
    assert r["verdict"] == "scam"
    assert {"offplatform_link", "delivery_arrangement_phrase", "receive_money_phrase"} <= set(
        r["signals"]
    )


def test_lone_offplatform_link_is_suspicious_not_scam() -> None:
    r = scam.scan(
        "check this out https://randomsite.top/deal",
        allowlist=_allowlist(),
        signatures=[],
        checkout_base=BASE,
    )
    assert r["verdict"] == "suspicious"


def test_genuine_marketplace_link_is_clean() -> None:
    r = scam.scan(
        "here it is https://www.facebook.com/marketplace/item/12345",
        allowlist=_allowlist(),
        signatures=[],
        checkout_base=BASE,
    )
    assert r["verdict"] == "clean"


def test_split_play_fires_via_history_window() -> None:
    r = scam.scan(
        "here https://fastpay.top/claim",
        history_text="Great, I'll arrange the courier for pickup",
        allowlist=_allowlist(),
        signatures=[],
        checkout_base=BASE,
    )
    assert r["verdict"] == "scam"  # delivery phrase in history + off-platform link now


def test_lookalike_checkout_flags_scam() -> None:
    r = scam.scan(
        "pay at https://api.carousell.ai.scam.site/checkout/x to receive the money",
        allowlist=_allowlist(),
        signatures=[],
        checkout_base=BASE,
    )
    assert r["links"][0]["checkout_link"] is False
    assert r["verdict"] == "scam"


def test_playbook_signature_needs_min_signals() -> None:
    sigs = [
        {
            "id": "sig-x",
            "kind": "playbook",
            "value": "courier_link",
            "signals": ["arrange the delivery", "click the link"],
            "min_signals": 2,
        }
    ]
    one = scam.scan(
        "please arrange the delivery", allowlist=_allowlist(), signatures=sigs, checkout_base=BASE
    )
    two = scam.scan(
        "i'll arrange the delivery, click the link",
        allowlist=_allowlist(),
        signatures=sigs,
        checkout_base=BASE,
    )
    assert "sig-x" not in one["bank_hits"]
    assert "sig-x" in two["bank_hits"]


# --- merged view + bank state machine ----------------------------------------------------------


def test_merge_registry_wins_and_dismissed_suppresses() -> None:
    registry = [{"id": "r1", "kind": "domain", "value": "fastpay.top", "severity": "high"}]
    bank = [
        {"id": "b1", "kind": "domain", "value": "fastpay.top", "status": "confirmed"},
        {"id": "b2", "kind": "domain", "value": "evil.win", "status": "observed"},
        {"id": "b3", "kind": "message_pattern", "value": "click to claim", "status": "dismissed"},
    ]
    merged = scam.merge_signatures(registry, bank)
    by_value = {m["value"]: m for m in merged}
    assert by_value["fastpay.top"]["source"] == "registry"  # registry wins the tie
    assert by_value["evil.win"]["source"] == "local"
    assert "click to claim" not in by_value  # dismissed suppressed


def test_store_bank_add_dedup_and_confirm(store) -> None:
    first = store.add_scam_signature(
        kind="domain", value="fastpay.top", marketplace="fb", thread_id="fb:1", context="x"
    )
    assert first["deduped"] is False
    again = store.add_scam_signature(
        kind="domain", value="fastpay.top", marketplace="fb", thread_id="fb:1", context="x"
    )
    assert again["deduped"] is True and again["id"] == first["id"]
    # a detect sighting is born observed; a seller-confirm is born confirmed
    seller = store.add_scam_signature(
        kind="domain",
        value="evil.win",
        marketplace="fb",
        thread_id="fb:1",
        context="x",
        detected_by="seller_confirm",
    )
    merged, ok = store.merged_scam_signatures()
    assert ok
    values = {m["value"] for m in merged}
    assert "fastpay.top" in values and "evil.win" in values  # both active in the merged view
    assert seller["id"]


def test_store_dismiss_suppresses_from_merged(store) -> None:
    added = store.add_scam_signature(
        kind="domain", value="evil.win", marketplace="fb", thread_id="fb:1", context="x"
    )
    store.transition_scam_signature(added["id"], "dismissed")
    merged, _ = store.merged_scam_signatures()
    assert "evil.win" not in {m["value"] for m in merged}


def test_store_illegal_transition_refused(store) -> None:
    added = store.add_scam_signature(
        kind="domain", value="evil.win", marketplace="fb", thread_id="fb:1", context="x"
    )
    with pytest.raises(StoreError):
        store.transition_scam_signature(added["id"], "shared")  # observed -> shared is illegal


def test_registry_unreadable_degrades_to_bank_only(store, monkeypatch) -> None:
    from sellee import marketplaces

    # the store reads marketplaces.SCAM_REGISTRY_PATH at call time — one patch covers both
    monkeypatch.setattr(
        marketplaces, "SCAM_REGISTRY_PATH", marketplaces.PACKAGE_DATA_DIR / "nope.json"
    )
    merged, ok = store.merged_scam_signatures()
    assert ok is False and merged == []


# --- tools ---------------------------------------------------------------------------------------


def _sell_thread(store):
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    store.create_thread(
        thread_id="fb:1", side="sell", market="fb", counterpart_handle="bob", item_id=item["id"]
    )
    return item


def test_scam_scan_tool_reads_thread_history(store, make_ctx) -> None:
    _sell_thread(store)
    store.append_thread_message(
        "fb:1", msg_id="m1", direction="in", text="Great, I'll arrange the courier", ts=1.0
    )
    ctx = make_ctx("attended")
    result = dispatch(
        "scam_scan",
        {"thread_id": "fb:1", "market": "fb", "text": "here https://fastpay.top/claim"},
        ctx,
    )
    assert result["verdict"] == "scam"  # the delivery phrase from history + the link now


def test_record_scam_refuses_marketplace_host(store, make_ctx) -> None:
    _sell_thread(store)
    ctx = make_ctx("attended")
    with pytest.raises(ToolError, match="marketplace host"):
        dispatch(
            "record_scam_signature",
            {
                "kind": "link",
                "value": "www.facebook.com",
                "market": "fb",
                "thread_id": "fb:1",
                "context": "x",
            },
            ctx,
        )


def test_record_then_scan_hits_then_retract(store, make_ctx) -> None:
    _sell_thread(store)
    ctx = make_ctx("attended")
    dispatch(
        "record_scam_signature",
        {
            "kind": "link",
            "value": "fastpay.top",
            "market": "fb",
            "thread_id": "fb:1",
            "context": "seen it",
        },
        ctx,
    )
    hit = dispatch(
        "scam_scan", {"thread_id": "fb:1", "market": "fb", "text": "go to fastpay.top now"}, ctx
    )
    assert hit["bank_hits"]  # the banked domain matched
    removed = dispatch("retract_scam_signature", {"thread_id": "fb:1"}, ctx)
    assert removed["removed"] == 1
    after, _ = store.merged_scam_signatures()
    assert "fastpay.top" not in {m["value"] for m in after}
