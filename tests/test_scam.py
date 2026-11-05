"""The scam engine + its tools: scoring, the merged registry+bank view, the bank state machine,
the record/retract/scan tools over the store, and the seller's answer to a carousell.ai scam
confirmation."""

from __future__ import annotations

import itertools
import shutil
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from tests.conftest import seed_setting
from tests.fake_carousell_ai_mcp import FakeRelay, serve

from sellee import migrations, reply_prompt, skills
from sellee.channel import asks
from sellee.config import Config
from sellee.db import Database
from sellee.engines import hosts, scam
from sellee.events import EventBus, EventStore
from sellee.rail import inbox as relay
from sellee.rail.client import RailClient, RailToolError
from sellee.rail.sink import RelayReplySink
from sellee.store import SCAM_CONFIRMATION, Store, StoreError
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_EDIT,
    TIER_PASS_PUBLISH,
    TIER_PASS_REPLY,
    Session,
    ToolContext,
    ToolError,
    all_specs,
    dispatch,
    tools_for_tier,
)

BASE = "https://www.carousell.ai/checkout"


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
    store.transition_scam_signature(added["id"], "dismissed")
    with pytest.raises(StoreError):
        store.transition_scam_signature(added["id"], "confirmed")  # dismissed is final


def test_shared_is_no_longer_a_signature_status(store) -> None:
    added = store.add_scam_signature(
        kind="domain",
        value="evil.win",
        marketplace="fb",
        thread_id="fb:1",
        context="x",
        detected_by="seller_confirm",
    )
    with pytest.raises(StoreError, match="unknown scam status"):
        store.transition_scam_signature(added["id"], "shared")


def test_the_scam_guard_says_a_carousell_ai_scam_is_reported() -> None:
    text = " ".join(skills.load("scam-guard").split())
    assert "blocks and reports the buyer to carousell.ai" in text
    assert "no tool here files a report" not in text
    assert "nothing you record travels" not in text.lower()


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


# --- the carousell.ai scam confirmation ----------------------------------------------------------

_WEB = "https://www.carousell.ai"
_FAST = Config(reply_delay_sec=(0, 0), interactive_reply_delay_sec=(0, 0))
# A buyer text for each verdict Sellee's own scan gives.
_LOCAL = {
    "clean": "Is the lamp still available?",
    "suspicious": "check this out https://randomsite.top/deal",
    "scam": "I'll arrange the courier, click https://fastpay-sg.top/claim to receive the money",
}


class _Relay:
    """One carousell.ai thread on a fresh store, with the carousell.ai fake behind it."""

    def __init__(self, root: Path):
        data_db, events_db = Database(root / "sellee.db"), Database(root / "events.db")
        migrations.run_startup_migrations(
            data_db=data_db, events_db=events_db, backups_dir=root / "b", backups_keep=1
        )
        self.dbs = (data_db, events_db)
        self.store = Store(data_db)
        seed_setting(self.store, "quiet_hours", [0, 0])
        self.bus = EventBus(EventStore(events_db))
        self.fake = FakeRelay()
        self.fake.base, self._shutdown = serve(self.fake)
        made = self.store.create_item(title="Teak lamp", list_price=80.0, currency="SGD")
        self.store.record_listing_url(made["id"], "carousell-ai", f"{_WEB}/listing/L1")
        self.fake.add_thread("t1", listing_id="L1")
        self.thread_id = "carousell-ai:t1"
        self._ids = itertools.count(1)
        self.pass_id: str | None = None

    def client(self) -> RailClient:
        return RailClient(api_base=self.fake.base, api_key="k", web_base_url=_WEB)

    def ctx(self, tier: str = TIER_ATTENDED, pass_id: str | None = None) -> ToolContext:
        sink = RelayReplySink(
            client=self.client(), store=self.store, bus=self.bus, retry_delays_sec=(0, 0, 0)
        )
        return ToolContext(
            session=Session(tier=tier, pass_id=pass_id),
            store=self.store,
            bus=self.bus,
            config=_FAST,
            rail_factory=self.client,
            reply_sink=lambda _market: sink,
        )

    def buyer_writes(self, *, local: str = "clean", risk: str = "", flags=()) -> str:
        msg_id = f"m{next(self._ids)}"
        self.fake.add_message("t1", msg_id, "buyer", _LOCAL[local], risk=risk, flags=flags)
        relay.relay_lane(
            relay.RelayDeps(
                store=self.store, bus=self.bus, config=Config(), rail_factory=self.client
            )
        )
        return msg_id

    def seller_sends(self, event: dict) -> str:
        """One Telegram or Discord update from the seller, resolved and ingested as the poller
        does, then claimed into a new channel pass (the previous one finished)."""
        if self.pass_id is not None:
            self.store.finish_pass(self.pass_id, status="done", cls="ok")
        event_id = next(self._ids) * 100
        [event] = asks.resolve_ask_answers(self.store, [{"event_id": event_id, **event}])
        self.store.ingest_updates([event], event_id + 1)
        self.pass_id = self.store.enqueue_channel_pass()
        return self.pass_id

    def seller_types(self, text: str) -> str:
        return self.seller_sends({"kind": "text", "text": text, "payload": {}})

    def seller_taps(self, notice_id: int, choice: str = "a0") -> str:
        payload = {"ref": f"n{notice_id}", "choice": choice}
        return self.seller_sends({"kind": "action", "text": choice, "payload": payload})

    def confirm(self, *, hold_first: bool = True) -> None:
        """The scam guard's handling: hold the thread and ask the seller."""
        ctx = self.ctx()
        hold = ("hold_thread", {"thread_id": self.thread_id, "reason": "scam"})
        ask = (
            "escalate",
            {
                "thread_id": self.thread_id,
                "open_question": "This buyer sent a payment link. Is it a scam?",
                "kind": SCAM_CONFIRMATION,
                "options": ["👍 Not a scam — resume", "🛑 Keep it held"],
            },
        )
        for name, params in (hold, ask) if hold_first else (ask, hold):
            dispatch(name, params, ctx)

    def answer(self, answer: str) -> dict:
        return dispatch(
            "answer_scam_confirmation", {"thread_id": self.thread_id, "answer": answer}, self.ctx()
        )

    def reply(self, msg_id: str) -> dict:
        return dispatch(
            "send_reply",
            {"thread_id": self.thread_id, "text": "Yes, still here.", "in_msg_id": msg_id},
            self.ctx(),
        )

    def messages(self) -> list:
        return self.store.get_thread_messages(self.thread_id)

    def close(self) -> None:
        self._shutdown()
        for db in self.dbs:
            db.close()


@pytest.fixture
def relay_thread(tmp_path, xdg_tmp):
    made = _Relay(tmp_path)
    yield made
    made.close()


def test_not_a_scam_lets_sellee_answer_the_buyer_again(relay_thread) -> None:
    msg_id = relay_thread.buyer_writes(risk="scam", flags=["platform_impersonation"])
    relay_thread.confirm()
    assert relay_thread.reply(msg_id)["status"] == "scam_held"
    calls = list(relay_thread.fake.tool_calls)

    result = relay_thread.answer("not_scam")

    assert result["status"] == "active"
    assert relay_thread.fake.tool_calls == calls
    [row] = relay_thread.messages()
    assert (row["scam_verdict"], row["carousell_ai_risk"]) == ("suspicious", "scam")
    assert row["carousell_ai_flags"] == ["platform_impersonation"]
    assert relay_thread.store.list_open_escalations() == []
    assert relay_thread.reply(msg_id)["status"] == "sent"


def test_the_reply_prompt_says_the_seller_called_it_not_a_scam(relay_thread) -> None:
    relay_thread.buyer_writes(risk="scam", flags=["platform_impersonation"])
    relay_thread.confirm()
    thread = relay_thread.store.get_thread
    held = reply_prompt.build_reply_prompt([thread(relay_thread.thread_id)], {})
    assert "said it is not a scam" not in held

    relay_thread.answer("not_scam")
    relay_thread.buyer_writes(local="clean", risk="clean")

    prompt = reply_prompt.build_reply_prompt([thread(relay_thread.thread_id)], {})
    assert "said it is not a scam" in prompt
    assert "do not ask the seller about those flags again" in prompt


def test_keep_it_held_does_not_say_the_seller_cleared_it(relay_thread) -> None:
    relay_thread.buyer_writes(risk="scam", flags=["platform_impersonation"])
    relay_thread.confirm()

    relay_thread.answer("keep_held")

    assert relay_thread.store.get_thread(relay_thread.thread_id)["scam_cleared"] is False


def test_not_a_scam_leaves_messages_that_were_not_scam_as_they_were(relay_thread) -> None:
    relay_thread.buyer_writes(local="clean", risk="clean")
    relay_thread.buyer_writes(local="suspicious")
    relay_thread.buyer_writes(local="scam", risk="clean")
    relay_thread.confirm(hold_first=False)

    relay_thread.answer("not_scam")

    assert [(r["scam_verdict"], r["carousell_ai_risk"]) for r in relay_thread.messages()] == [
        ("clean", "clean"),
        ("suspicious", None),
        ("suspicious", "clean"),
    ]
    assert relay_thread.store.get_thread(relay_thread.thread_id)["status"] == "active"


def test_not_a_scam_resumes_after_the_scam_was_handled_twice(relay_thread) -> None:
    msg_id = relay_thread.buyer_writes(risk="scam")
    relay_thread.confirm()
    relay_thread.confirm()

    assert relay_thread.answer("not_scam")["status"] == "active"
    assert relay_thread.reply(msg_id)["status"] == "sent"


def test_an_ask_on_a_thread_holding_a_scam_is_a_scam_confirmation(relay_thread) -> None:
    relay_thread.buyer_writes(risk="scam")
    dispatch(
        "escalate",
        {"thread_id": relay_thread.thread_id, "open_question": "Scam?"},
        relay_thread.ctx(),
    )

    [esc] = relay_thread.store.list_open_escalations()
    assert esc["kind"] == SCAM_CONFIRMATION
    assert relay_thread.answer("not_scam")["status"] == "active"


def test_an_ask_opened_before_the_kind_existed_is_still_answered(relay_thread) -> None:
    msg_id = relay_thread.buyer_writes(risk="scam")
    relay_thread.store.hold_thread(relay_thread.thread_id, "scam")
    relay_thread.store.escalate(relay_thread.thread_id, open_question="Scam?")

    assert relay_thread.answer("not_scam")["status"] == "active"
    assert relay_thread.reply(msg_id)["status"] == "sent"


def test_keep_it_held_closes_the_ask_and_keeps_the_hold(relay_thread) -> None:
    msg_id = relay_thread.buyer_writes(risk="scam")
    relay_thread.confirm(hold_first=False)

    assert relay_thread.answer("keep_held")["status"] == "held"

    assert relay_thread.store.list_open_escalations() == []
    assert relay_thread.messages()[0]["scam_verdict"] == "scam"
    assert relay_thread.reply(msg_id)["status"] == "scam_held"
    dispatch("release_thread", {"thread_id": relay_thread.thread_id}, relay_thread.ctx())
    assert relay_thread.store.get_thread(relay_thread.thread_id)["status"] == "active"


@pytest.mark.parametrize("tool", ["release_thread", "resolve_escalation"])
def test_a_scam_confirmation_is_answered_only_by_its_own_tool(relay_thread, tool) -> None:
    relay_thread.buyer_writes(risk="scam")
    relay_thread.confirm()
    [esc] = relay_thread.store.list_open_escalations()
    params = {
        "release_thread": {"thread_id": relay_thread.thread_id},
        "resolve_escalation": {"escalation_id": esc["id"], "resolution": "fine"},
    }[tool]

    with pytest.raises(ToolError, match="answer_scam_confirmation"):
        dispatch(tool, params, relay_thread.ctx())

    assert relay_thread.store.get_thread(relay_thread.thread_id)["status"] == "escalated"
    assert relay_thread.store.list_open_escalations() == [esc]


def test_an_answer_with_no_confirmation_open_changes_nothing(relay_thread) -> None:
    msg_id = relay_thread.buyer_writes(risk="scam")
    relay_thread.store.hold_thread(relay_thread.thread_id, "scam")

    with pytest.raises(ToolError, match="no scam confirmation"):
        relay_thread.answer("not_scam")

    assert relay_thread.messages()[0]["scam_verdict"] == "scam"
    assert relay_thread.store.get_thread(relay_thread.thread_id)["status"] == "held"
    assert relay_thread.reply(msg_id)["status"] == "scam_held"


def test_another_markets_confirmation_is_not_answered_here(store, make_ctx) -> None:
    _sell_thread(store)
    store.append_thread_message(
        "fb:1", msg_id="m1", direction="in", text="x", ts=1.0, scam_verdict="scam"
    )
    store.escalate("fb:1", open_question="Scam?", kind=SCAM_CONFIRMATION)

    with pytest.raises(ToolError, match="carousell.ai"):
        dispatch(
            "answer_scam_confirmation",
            {"thread_id": "fb:1", "answer": "not_scam"},
            make_ctx("attended"),
        )

    assert store.get_thread_messages("fb:1")[0]["scam_verdict"] == "scam"
    [esc] = store.list_open_escalations()
    dispatch(
        "resolve_escalation", {"escalation_id": esc["id"], "resolution": "no"}, make_ctx("attended")
    )
    dispatch("release_thread", {"thread_id": "fb:1"}, make_ctx("attended"))


def test_scam_blocks_and_reports_the_buyer_in_one_tap(relay_thread) -> None:
    relay_thread.buyer_writes(risk="scam", flags=["platform_impersonation"])
    relay_thread.confirm()

    result = relay_thread.answer("scam")

    assert result["status"] == "reported"
    assert relay_thread.fake.block_calls == [{"id": "t1", "reason": "scam"}]
    assert relay_thread.fake.reports == {"t1": "scam"}
    assert relay_thread.store.get_thread(relay_thread.thread_id)["status"] == "closed"
    assert relay_thread.store.list_open_escalations() == []
    recorded = dispatch(
        "record_scam_signature",
        {
            "kind": "link",
            "value": "fastpay-sg.top",
            "market": "carousell-ai",
            "thread_id": relay_thread.thread_id,
            "context": "seller confirmed",
            "source": "seller_confirm",
        },
        relay_thread.ctx(),
    )
    assert recorded["deduped"] is False
    assert len(relay_thread.fake.block_calls) == 1


@pytest.mark.parametrize("failure", ["http503", "internal"])
def test_a_report_that_does_not_reach_carousell_ai_keeps_the_thread_held(
    relay_thread, failure
) -> None:
    relay_thread.buyer_writes(risk="scam")
    relay_thread.confirm()
    relay_thread.fake.block_script = [failure]

    failed = relay_thread.answer("scam")

    assert failed["status"] == "report_failed"
    # A timeout can follow a block bazaar committed, so the seller is told only what is known.
    assert "did not confirm" in failed["detail"]
    assert "not blocked" not in failed["detail"]
    assert relay_thread.store.get_thread(relay_thread.thread_id)["status"] == "escalated"
    assert len(relay_thread.store.list_open_escalations()) == 1
    assert relay_thread.messages()[0]["scam_verdict"] == "scam"
    assert relay_thread.answer("scam")["status"] == "reported"
    assert relay_thread.fake.block_calls == [{"id": "t1", "reason": "scam"}] * 2


def test_an_unprovisioned_rail_reports_nothing_and_keeps_the_thread_held(relay_thread) -> None:
    relay_thread.buyer_writes(risk="scam")
    relay_thread.confirm()
    ctx = relay_thread.ctx()
    ctx.rail_factory = None

    result = dispatch(
        "answer_scam_confirmation", {"thread_id": relay_thread.thread_id, "answer": "scam"}, ctx
    )

    assert result["status"] == "report_failed"
    assert relay_thread.store.get_thread(relay_thread.thread_id)["status"] == "escalated"


@pytest.mark.parametrize("source", ["detect", "seller_confirm"])
def test_recording_a_signature_sends_no_report(relay_thread, source) -> None:
    relay_thread.buyer_writes(risk="scam")
    dispatch(
        "record_scam_signature",
        {
            "kind": "pattern",
            "value": "receive the money",
            "market": "carousell-ai",
            "thread_id": relay_thread.thread_id,
            "context": "x",
            "source": source,
        },
        relay_thread.ctx(),
    )

    assert relay_thread.fake.block_calls == []


def test_a_carousell_ai_scam_confirmation_offers_block_and_report(relay_thread) -> None:
    relay_thread.buyer_writes(risk="scam")
    relay_thread.confirm()

    [esc] = relay_thread.store.list_open_escalations()
    assert esc["options"] == list(asks.CAROUSELL_AI_SCAM_OPTIONS)
    assert asks.validate_options(list(asks.CAROUSELL_AI_SCAM_OPTIONS))


def test_another_markets_scam_confirmation_is_unchanged(store, make_ctx) -> None:
    _sell_thread(store)
    store.append_thread_message(
        "fb:1", msg_id="m1", direction="in", text="x", ts=1.0, scam_verdict="scam"
    )
    options = ["👍 Not a scam — resume", "🛑 Keep it held"]
    dispatch(
        "escalate",
        {"thread_id": "fb:1", "open_question": "Scam?", "options": options},
        make_ctx("attended"),
    )

    [esc] = store.list_open_escalations()
    assert (esc["kind"], esc["options"]) == (None, options)
    with pytest.raises(ToolError, match="carousell.ai"):
        dispatch(
            "answer_scam_confirmation",
            {"thread_id": "fb:1", "answer": "scam"},
            make_ctx("attended"),
        )


@pytest.mark.parametrize("reason", ["", "SCAM", "fraud", None])
def test_the_rail_client_sends_no_block_without_a_known_reason(relay_thread, reason) -> None:
    with pytest.raises(RailToolError, match="reason"):
        relay_thread.client().block_buyer("t1", reason)

    assert relay_thread.fake.block_calls == []


def test_the_reply_pass_cannot_answer_a_scam_confirmation() -> None:
    [spec] = [s for s in all_specs() if s.name == "answer_scam_confirmation"]
    assert TIER_PASS_REPLY not in spec.tiers


# --- the attended block tool --------------------------------------------------------------------


def _block(relay_thread, reason, tier: str = TIER_ATTENDED) -> dict:
    return dispatch(
        "carousell_ai_block_buyer",
        {"thread_id": relay_thread.thread_id, "reason": reason},
        relay_thread.ctx(tier, relay_thread.pass_id),
    )


def _ask_from_chat(relay_thread, reason) -> int:
    """The channel pass, on the seller's request, asks them to confirm a block."""
    relay_thread.seller_types(f"block that buyer for {reason}")
    return _block(relay_thread, reason, TIER_PASS_CHANNEL)["notice_id"]


def _answer(relay_thread, notice_id) -> dict:
    return dispatch(
        "answer_block_confirmation",
        {"notice_id": notice_id},
        relay_thread.ctx(TIER_PASS_CHANNEL, relay_thread.pass_id),
    )


def test_the_seller_can_block_a_buyer_for_spam(relay_thread) -> None:
    relay_thread.buyer_writes()

    result = _block(relay_thread, "spam")

    assert result["status"] == "blocked"
    assert relay_thread.fake.block_calls == [{"id": "t1", "reason": "spam"}]
    assert relay_thread.fake.reports == {"t1": "spam"}
    assert relay_thread.store.get_thread(relay_thread.thread_id)["status"] == "closed"


def test_a_second_block_claims_no_reason_bazaar_did_not_record(relay_thread) -> None:
    relay_thread.buyer_writes()
    _block(relay_thread, "spam")

    again = _block(relay_thread, "abuse")

    assert again == {"thread_id": relay_thread.thread_id, "status": "blocked"}
    assert relay_thread.fake.reports == {"t1": "spam"}


@pytest.mark.parametrize("reason", ["scam", "SCAM", "fraud", ""])
def test_the_block_tool_refuses_scam_and_unknown_reasons(relay_thread, reason) -> None:
    relay_thread.buyer_writes()

    with pytest.raises(ToolError):
        _block(relay_thread, reason)

    assert relay_thread.fake.block_calls == []
    assert relay_thread.store.get_thread(relay_thread.thread_id)["status"] == "active"


def test_a_block_carousell_ai_did_not_confirm_changes_nothing(relay_thread) -> None:
    relay_thread.buyer_writes()
    relay_thread.fake.block_script = ["http503"]

    result = _block(relay_thread, "abuse")

    assert (result["thread_id"], result["status"]) == (relay_thread.thread_id, "block_failed")
    assert "did not confirm" in result["detail"]
    assert relay_thread.store.get_thread(relay_thread.thread_id)["status"] == "active"


def test_another_markets_buyer_is_not_blocked_here(store, make_ctx) -> None:
    _sell_thread(store)

    with pytest.raises(ToolError, match="carousell.ai"):
        dispatch(
            "carousell_ai_block_buyer",
            {"thread_id": "fb:1", "reason": "spam"},
            make_ctx("attended"),
        )


@pytest.mark.parametrize("tier", [TIER_PASS_REPLY, TIER_PASS_PUBLISH, TIER_PASS_EDIT])
def test_unattended_tiers_cannot_see_the_block_tools(tier) -> None:
    names = {spec.name for spec in tools_for_tier(tier)}
    assert not {"carousell_ai_block_buyer", "answer_block_confirmation"} & names
    for seller_tier in (TIER_ATTENDED, TIER_PASS_CHANNEL):
        assert "carousell_ai_block_buyer" in {spec.name for spec in tools_for_tier(seller_tier)}
    assert "answer_block_confirmation" in {spec.name for spec in tools_for_tier(TIER_PASS_CHANNEL)}


# --- the block from Telegram or Discord, behind the seller's tap --------------------------------


def _ask_notice(relay_thread, notice_id) -> dict:
    [notice] = [n for n in relay_thread.store.list_queued_notices() if n["id"] == notice_id]
    return notice


@pytest.mark.parametrize("reason", ["spam", "abuse", "other"])
def test_a_chat_block_asks_the_seller_and_sends_nothing(relay_thread, reason) -> None:
    relay_thread.buyer_writes()
    relay_thread.seller_types("block that buyer")

    result = _block(relay_thread, reason, TIER_PASS_CHANNEL)

    assert result == {
        "thread_id": relay_thread.thread_id,
        "status": "asked",
        "notice_id": result["notice_id"],
    }
    assert relay_thread.fake.block_calls == []
    assert relay_thread.store.get_thread(relay_thread.thread_id)["status"] == "active"
    notice = _ask_notice(relay_thread, result["notice_id"])
    assert notice["text"].startswith("Block and report Alice (Carousell.ai · Teak lamp) to ")
    assert f"for {reason}?" in notice["text"]
    assert [label for label, _ in notice["controls"]] == [
        f"🚫 Block Alice: {reason}",
        "Don't block",
    ]


@pytest.mark.parametrize("handle", ["a" * 40, "李" * 20, "Bob"])
def test_a_block_buttons_names_the_buyer_and_fits_a_phone(handle) -> None:
    for reason in ("spam", "abuse", "other"):
        block, _ = asks.block_options(reason, handle)
        assert asks.validate_options([block, "Don't block"])
        assert block.startswith("🚫 Block ") and block.endswith(f": {reason}")
        assert handle[:3] in block


@pytest.mark.parametrize("reason", ["spam", "abuse", "other"])
def test_the_sellers_tap_sends_one_block_for_the_buyer_and_reason_asked(
    relay_thread, reason
) -> None:
    relay_thread.buyer_writes()
    notice_id = _ask_from_chat(relay_thread, reason)
    relay_thread.seller_taps(notice_id)

    result = _answer(relay_thread, notice_id)

    assert result == {"thread_id": relay_thread.thread_id, "status": "blocked"}
    assert relay_thread.fake.block_calls == [{"id": "t1", "reason": reason}]
    assert relay_thread.store.get_thread(relay_thread.thread_id)["status"] == "closed"
    with pytest.raises(ToolError, match="tap"):
        _answer(relay_thread, notice_id)
    assert len(relay_thread.fake.block_calls) == 1


def test_two_answers_racing_on_one_tap_send_one_block(relay_thread, monkeypatch) -> None:
    relay_thread.buyer_writes()
    notice_id = _ask_from_chat(relay_thread, "spam")
    relay_thread.seller_taps(notice_id)
    # The second answer read the tap before the first one spent it.
    seen = relay_thread.store.inbox_for_pass(relay_thread.pass_id)
    _answer(relay_thread, notice_id)
    monkeypatch.setattr(relay_thread.store, "inbox_for_pass", lambda _pass_id: seen)

    with pytest.raises(ToolError, match="tap"):
        _answer(relay_thread, notice_id)

    assert relay_thread.fake.block_calls == [{"id": "t1", "reason": "spam"}]


def test_a_chat_block_refuses_scam_before_asking(relay_thread) -> None:
    relay_thread.buyer_writes()
    relay_thread.seller_types("block that scammer")

    with pytest.raises(ToolError):
        _block(relay_thread, "scam", TIER_PASS_CHANNEL)

    assert relay_thread.store.list_queued_notices() == []
    assert relay_thread.fake.block_calls == []


def _no_tap(relay_thread, notice_id) -> None:
    pass


def _dont_block(relay_thread, notice_id) -> None:
    relay_thread.seller_taps(notice_id, "a1")


def _typed_label(relay_thread, notice_id) -> None:
    relay_thread.seller_types("🚫 Block for spam")


def _tap_in_an_earlier_turn(relay_thread, notice_id) -> None:
    relay_thread.seller_taps(notice_id)
    relay_thread.seller_types("thanks")


def _tap_on_another_ask(relay_thread, notice_id) -> None:
    other = dispatch(
        "send_message",
        {"text": "Block them?", "options": ["🚫 Block for spam", "Don't block"]},
        relay_thread.ctx(TIER_PASS_CHANNEL, relay_thread.pass_id),
    )["notice_id"]
    relay_thread.seller_taps(other)


@pytest.mark.parametrize(
    "before", [_no_tap, _dont_block, _typed_label, _tap_in_an_earlier_turn, _tap_on_another_ask]
)
def test_only_the_sellers_block_tap_this_turn_sends_a_block(relay_thread, before) -> None:
    relay_thread.buyer_writes()
    notice_id = _ask_from_chat(relay_thread, "spam")
    before(relay_thread, notice_id)

    with pytest.raises(ToolError, match="tap"):
        _answer(relay_thread, notice_id)

    assert relay_thread.fake.block_calls == []
    assert relay_thread.store.get_thread(relay_thread.thread_id)["status"] == "active"


def test_a_tap_on_a_message_that_is_not_a_block_ask_sends_nothing(relay_thread) -> None:
    relay_thread.buyer_writes()
    relay_thread.seller_types("hi")
    other = dispatch(
        "send_message",
        {"text": "Block them?", "options": ["🚫 Block for spam", "Don't block"]},
        relay_thread.ctx(TIER_PASS_CHANNEL, relay_thread.pass_id),
    )["notice_id"]
    relay_thread.seller_taps(other)

    with pytest.raises(ToolError, match="block ask"):
        _answer(relay_thread, other)

    assert relay_thread.fake.block_calls == []


def test_the_model_cannot_write_a_block_ask_of_its_own(relay_thread) -> None:
    relay_thread.seller_types("hi")

    with pytest.raises(ToolError, match="block:"):
        dispatch(
            "send_message",
            {
                "text": "Tap to see the lamp",
                "options": ["🚫 Block for spam", "Don't block"],
                "ref": f"block:spam:{relay_thread.thread_id}",
            },
            relay_thread.ctx(TIER_PASS_CHANNEL, relay_thread.pass_id),
        )

    assert relay_thread.store.list_queued_notices() == []


def test_a_failed_block_sends_a_fresh_ask_whose_tap_sends_it_again(relay_thread) -> None:
    relay_thread.buyer_writes()
    notice_id = _ask_from_chat(relay_thread, "abuse")
    relay_thread.seller_taps(notice_id)
    relay_thread.fake.block_script = ["http503"]
    before = {n["id"] for n in relay_thread.store.list_queued_notices()}

    failed = _answer(relay_thread, notice_id)

    assert (failed["status"], failed["thread_id"]) == ("block_failed", relay_thread.thread_id)
    assert "new ask" in failed["detail"]
    assert relay_thread.store.get_thread(relay_thread.thread_id)["status"] == "active"
    [fresh] = [n for n in relay_thread.store.list_queued_notices() if n["id"] not in before]
    first = _ask_notice(relay_thread, notice_id)
    assert (fresh["ref"], fresh["text"]) == (first["ref"], first["text"])
    assert [label for label, _ in fresh["controls"]] == [label for label, _ in first["controls"]]
    relay_thread.seller_taps(fresh["id"])
    assert _answer(relay_thread, fresh["id"])["status"] == "blocked"
    assert relay_thread.fake.block_calls[-1] == {"id": "t1", "reason": "abuse"}


def test_an_escalation_on_a_lookalike_thread_is_not_a_block_ask(relay_thread) -> None:
    relay_thread.buyer_writes()
    relay_thread.seller_types("hi")
    ctx = relay_thread.ctx(TIER_PASS_CHANNEL, relay_thread.pass_id)
    # An escalation's notice takes its thread's id as its ref, and the model names threads.
    lookalike = f"block:spam:{relay_thread.thread_id}"
    dispatch(
        "create_thread",
        {
            "thread_id": lookalike,
            "side": "sell",
            "market": "block",
            "counterpart_handle": "x",
            "item_id": relay_thread.store.list_items()[0]["id"],
        },
        ctx,
    )
    dispatch(
        "escalate",
        {
            "thread_id": lookalike,
            "open_question": "Want the lamp photos?",
            "options": ["Yes please", "No thanks"],
        },
        ctx,
    )
    _deliver_escalations(relay_thread)
    [notice] = [n for n in relay_thread.store.list_queued_notices() if n["ref"] == lookalike]
    relay_thread.seller_taps(notice["id"])

    with pytest.raises(ToolError, match="block ask"):
        _answer(relay_thread, notice["id"])

    assert relay_thread.fake.block_calls == []


def _deliver_escalations(relay_thread) -> None:
    """Queue each open escalation's notice, as the channel's escalation listener does."""
    from sellee.channel import outbound

    on_open = outbound.escalation_notifier(relay_thread.store)
    for esc in relay_thread.store.list_open_escalations():
        on_open(type("E", (), {"kind": "escalation.open", "payload": {"id": esc["id"]}})())


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    reason=st.sampled_from(["scam", "spam", "abuse", "other", "Scam", " scam", "scam "])
    | st.text(max_size=8),
    tier=st.sampled_from([TIER_ATTENDED, TIER_PASS_CHANNEL]),
)
def test_property_the_block_tool_never_sends_scam(relay_thread, reason, tier) -> None:
    """Property: on either tier and whatever the reason, no block_buyer carries `scam`, and one
    is sent exactly when the reason is spam, abuse or other (on the channel pass, once tapped)."""
    if relay_thread.store.get_thread(relay_thread.thread_id) is None:
        relay_thread.buyer_writes()
    sent = len(relay_thread.fake.block_calls)
    try:
        if tier == TIER_PASS_CHANNEL:
            notice_id = _ask_from_chat(relay_thread, reason)
            relay_thread.seller_taps(notice_id)
            _answer(relay_thread, notice_id)
        else:
            _block(relay_thread, reason)
    except ToolError:
        pass

    calls = relay_thread.fake.block_calls[sent:]
    assert all(call["reason"] != "scam" for call in relay_thread.fake.block_calls)
    assert calls == (
        [{"id": "t1", "reason": reason}] if reason in ("spam", "abuse", "other") else []
    )


_STEP = st.one_of(
    st.tuples(st.just("ask"), st.sampled_from(["spam", "abuse", "other"])),
    st.tuples(st.just("tap"), st.integers(0, 3), st.sampled_from(["a0", "a1"])),
    st.tuples(st.just("type"), st.sampled_from(["block them", "🚫 Block for spam", "yes"])),
    st.tuples(st.just("answer"), st.integers(0, 3)),
)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(steps=st.lists(_STEP, max_size=12))
def test_property_only_a_tap_sends_a_chat_block(tmp_path_factory, xdg_tmp, steps) -> None:
    """Property: on the channel pass each block_buyer follows one Block tap on a code-made ask in
    that turn and carries the reason the ask named; model calls alone never send one."""
    made = _Relay(tmp_path_factory.mktemp("relay"))
    try:
        made.buyer_writes()
        made.seller_types("hello")
        asked: list = []  # (notice_id, reason)
        tapped: dict = {}  # this turn's unanswered Block tap: notice_id -> reason
        expected: list = []
        for step in steps:
            if step[0] == "ask":
                try:
                    asked.append((_block(made, step[1], TIER_PASS_CHANNEL)["notice_id"], step[1]))
                except ToolError:
                    pass
            elif step[0] == "tap" and asked:
                notice_id, reason = asked[step[1] % len(asked)]
                made.seller_taps(notice_id, step[2])
                tapped = {notice_id: reason} if step[2] == "a0" else {}
            elif step[0] == "type":
                made.seller_types(step[1])
                tapped = {}
            elif step[0] == "answer" and asked:
                notice_id, _ = asked[step[1] % len(asked)]
                try:
                    _answer(made, notice_id)
                except ToolError:
                    pass
                if notice_id in tapped:
                    expected.append({"id": "t1", "reason": tapped.pop(notice_id)})
        assert made.fake.block_calls == expected
    finally:
        made.close()


_VERDICTS = ("clean", "suspicious", "scam")


class ScamConfirmation(RuleBasedStateMachine):
    """Buyer mail, confirmations and answers in any order on one carousell.ai thread. After
    *Not a scam* no message reads `scam` and no reply is refused as one; each *Scam* tap sends
    exactly one `block_buyer` with `scam`, and recording a signature sends none."""

    def __init__(self):
        super().__init__()
        self.root = Path(tempfile.mkdtemp())
        self.relay = _Relay(self.root)
        self.served: dict = {}
        self.open = False
        self.closed = False

    def teardown(self):
        self.relay.close()
        shutil.rmtree(self.root, ignore_errors=True)

    @rule(
        local=st.sampled_from(_VERDICTS),
        risk=st.sampled_from(("", "clean", "suspicious", "scam")),
        flags=st.lists(st.sampled_from(("scam_phrase", "off_platform_link")), max_size=2),
    )
    def buyer_writes(self, local, risk, flags):
        msg_id = self.relay.buyer_writes(local=local, risk=risk, flags=flags)
        self.served[msg_id] = risk or None

    @precondition(lambda self: self.served and not self.closed)
    @rule(hold_first=st.booleans())
    def sellee_asks_the_seller(self, hold_first):
        self.relay.confirm(hold_first=hold_first)
        self.open = True

    @precondition(lambda self: self.open)
    @rule()
    def seller_answers_not_a_scam(self):
        before = {row["msg_id"]: row["scam_verdict"] for row in self.relay.messages()}
        calls = list(self.relay.fake.tool_calls)

        self.relay.answer("not_scam")
        self.open = False

        assert self.relay.fake.tool_calls == calls
        after = {row["msg_id"]: row["scam_verdict"] for row in self.relay.messages()}
        assert after == {
            msg_id: "suspicious" if verdict == "scam" else verdict
            for msg_id, verdict in before.items()
        }
        for msg_id in self.served:
            assert self.relay.reply(msg_id)["status"] != "scam_held"

    @precondition(lambda self: self.open)
    @rule()
    def seller_answers_keep_it_held(self):
        before = self.relay.messages()

        assert self.relay.answer("keep_held")["status"] == "held"
        self.open = False

        assert self.relay.messages() == before
        assert self.relay.store.list_open_escalations() == []

    @precondition(lambda self: self.open)
    @rule(failure=st.sampled_from([None, "http503", "internal"]))
    def seller_answers_scam(self, failure):
        before = (self.relay.messages(), self.relay.store.get_thread(self.relay.thread_id))
        calls = len(self.relay.fake.block_calls)
        self.relay.fake.block_script = [failure] if failure else []

        result = self.relay.answer("scam")

        assert self.relay.fake.block_calls[calls:] == [{"id": "t1", "reason": "scam"}]
        if failure:
            assert result["status"] == "report_failed"
            assert (self.relay.messages(), self.relay.store.get_thread(self.relay.thread_id)) == (
                before
            )
            return
        assert result["status"] == "reported"
        assert self.relay.store.get_thread(self.relay.thread_id)["status"] == "closed"
        assert self.relay.store.list_open_escalations() == []
        self.open, self.closed = False, True

    @precondition(lambda self: self.served)
    @rule(source=st.sampled_from(["detect", "seller_confirm"]))
    def a_signature_is_recorded(self, source):
        calls = list(self.relay.fake.block_calls)
        dispatch(
            "record_scam_signature",
            {
                "kind": "pattern",
                "value": "receive the money",
                "market": "carousell-ai",
                "thread_id": self.relay.thread_id,
                "context": "x",
                "source": source,
            },
            self.relay.ctx(),
        )
        assert self.relay.fake.block_calls == calls

    @precondition(lambda self: not self.open)
    @rule()
    def seller_answers_with_nothing_asked(self):
        before = self.relay.messages()
        with pytest.raises(ToolError):
            self.relay.answer("not_scam")
        assert self.relay.messages() == before

    @invariant()
    def carousell_ais_risk_is_kept_as_the_record(self):
        stored = {
            row["msg_id"]: row["carousell_ai_risk"]
            for row in self.relay.messages()
            if row["dir"] == "in"
        }
        assert stored == self.served


ScamConfirmation.TestCase.settings = settings(
    max_examples=20,
    stateful_step_count=8,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


@pytest.mark.usefixtures("xdg_tmp")
class TestScamConfirmation(ScamConfirmation.TestCase):
    pass
