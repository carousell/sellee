"""The scam engine + its tools: scoring, the merged registry+bank view, the bank state machine,
the record/retract/scan tools over the store, and the seller's answer to a carousell.ai scam
confirmation."""

from __future__ import annotations

import itertools
import shutil
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from tests.conftest import seed_setting
from tests.fake_carousell_ai_mcp import FakeRelay, serve

from sellee import migrations
from sellee.config import Config
from sellee.db import Database
from sellee.engines import hosts, scam
from sellee.events import EventBus, EventStore
from sellee.rail import inbox as relay
from sellee.rail.client import RailClient
from sellee.rail.sink import RelayReplySink
from sellee.store import SCAM_CONFIRMATION, Store, StoreError
from sellee.tools.registry import (
    TIER_ATTENDED,
    TIER_PASS_REPLY,
    Session,
    ToolContext,
    ToolError,
    all_specs,
    dispatch,
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

    def client(self) -> RailClient:
        return RailClient(api_base=self.fake.base, api_key="k", web_base_url=_WEB)

    def ctx(self, tier: str = TIER_ATTENDED) -> ToolContext:
        sink = RelayReplySink(
            client=self.client(), store=self.store, bus=self.bus, retry_delays_sec=(0, 0, 0)
        )
        return ToolContext(
            session=Session(tier=tier),
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


def test_the_reply_pass_cannot_answer_a_scam_confirmation() -> None:
    [spec] = [s for s in all_specs() if s.name == "answer_scam_confirmation"]
    assert TIER_PASS_REPLY not in spec.tiers


_VERDICTS = ("clean", "suspicious", "scam")


class NotAScam(RuleBasedStateMachine):
    """Buyer mail, confirmations and answers in any order on one carousell.ai thread. After
    *Not a scam* no message reads `scam`, and no reply to any of them is refused as one."""

    def __init__(self):
        super().__init__()
        self.root = Path(tempfile.mkdtemp())
        self.relay = _Relay(self.root)
        self.served: dict = {}
        self.open = False

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

    @precondition(lambda self: self.served)
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


NotAScam.TestCase.settings = settings(
    max_examples=20,
    stateful_step_count=8,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


@pytest.mark.usefixtures("xdg_tmp")
class TestNotAScam(NotAScam.TestCase):
    pass
