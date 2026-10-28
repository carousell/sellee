"""An item's carousell.ai category: a token from the item policy the rail serves, read once and
kept until carousell.ai gives a sign it has changed."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from tests.fake_carousell_ai_mcp import FakeRelay, item_policy, serve

import sellee.tools  # noqa: F401  registration
from sellee import migrations
from sellee.config import Config
from sellee.db import Database
from sellee.events import EventBus, EventStore
from sellee.rail import client as rail_client
from sellee.rail.client import RailClient, RailPolicyRefused, RailToolRefused, RailUnprovisioned
from sellee.store import Store
from sellee.tools.registry import TIER_ATTENDED, Session, ToolContext, ToolError, dispatch

_WEB = "https://www.carousell.ai"


@pytest.fixture(autouse=True)
def _no_policy_from_another_test(monkeypatch):
    monkeypatch.setattr(rail_client, "_POLICIES", {})


@pytest.fixture
def rail():
    relay = FakeRelay()
    base, shutdown = serve(relay)
    relay.base = base
    yield relay
    shutdown()


def _factory(relay):
    # A new client per call, as the daemon's factory builds them.
    return lambda: RailClient(api_base=relay.base, api_key="k", web_base_url=_WEB)


@pytest.fixture
def ctx(make_ctx, rail):
    return make_ctx(TIER_ATTENDED, rail_factory=_factory(rail))


def _create(ctx, **params):
    return dispatch("create_item", {"title": "Lamp", "list_price": 40.0, **params}, ctx)


def _update(ctx, item_id, **fields):
    return dispatch("update_item", {"item_id": item_id, "fields": fields}, ctx)


# --- the item tools take a token the served policy lists ---------------------------------------


def test_an_item_keeps_the_category_it_was_created_with(ctx):
    item = _create(ctx, category="electronics")
    assert item["category"] == "electronics"
    assert dispatch("get_item", {"item_id": item["id"]}, ctx)["category"] == "electronics"


def test_a_refused_category_is_still_an_honest_one_to_keep(ctx):
    assert _create(ctx, category="weapons")["category"] == "weapons"
    assert _create(ctx, category="too_big_to_ship")["category"] == "too_big_to_ship"


def test_an_item_with_no_category_reads_no_policy(ctx, rail):
    item = _create(ctx)
    assert item["category"] is None
    _update(ctx, item["id"], title="Teak lamp")
    assert rail.policy_reads == 0


def test_a_token_the_policy_does_not_list_is_refused_with_where_to_look(ctx, store):
    with pytest.raises(ToolError) as exc:
        _create(ctx, category="crafts")
    message = str(exc.value)
    assert "'crafts'" in message
    assert "carousell_ai_item_policy" in message
    assert f"{_WEB}/item-policy" in message
    assert store.list_items() == []


def test_an_update_to_an_unlisted_token_is_refused_and_changes_nothing(ctx):
    item = _create(ctx, category="electronics")
    with pytest.raises(ToolError, match="carousell_ai_item_policy"):
        _update(ctx, item["id"], category="Electronics", title="Teak lamp")
    kept = dispatch("get_item", {"item_id": item["id"]}, ctx)
    assert (kept["category"], kept["title"]) == ("electronics", "Lamp")


def test_an_update_sets_and_clears_the_category(ctx):
    item = _create(ctx)
    assert _update(ctx, item["id"], category="home_and_garden")["category"] == "home_and_garden"
    assert _update(ctx, item["id"], category="")["category"] is None


def test_a_category_on_a_listed_item_changes_the_record(ctx, store):
    item = _create(ctx, category="electronics")
    store.record_listing_url(item["id"], "carousell_ai", f"{_WEB}/listing/1")
    assert _update(ctx, item["id"], category="home_and_garden")["category"] == "home_and_garden"


def test_a_category_is_refused_when_carousell_ai_is_not_set_up(make_ctx, store):
    def unprovisioned():
        raise RailUnprovisioned("carousell.ai is not provisioned")

    ctx = make_ctx(TIER_ATTENDED, rail_factory=unprovisioned)
    with pytest.raises(ToolError, match="leave it out"):
        _create(ctx, category="electronics")
    assert store.list_items() == []
    assert _create(ctx)["category"] is None


def test_a_category_is_refused_when_the_policy_cannot_be_read(ctx, rail, store):
    rail.down = True
    with pytest.raises(ToolError, match="leave it out"):
        _create(ctx, category="electronics")
    assert store.list_items() == []


def test_the_model_reads_every_category_with_its_tier(ctx):
    policy = dispatch("carousell_ai_item_policy", {}, ctx)
    assert policy["version"] == "2026-10-10.b2c32a7a9a46"
    assert policy["page"] == f"{_WEB}/item-policy"
    by_token = {c["token"]: c for c in policy["categories"]}
    assert {t: c["tier"] for t, c in by_token.items()} == {
        "electronics": "allowed",
        "home_and_garden": "allowed",
        "too_big_to_ship": "not_yet",
        "weapons": "never",
        "drugs_and_tobacco": "never",
    }
    assert by_token["weapons"] == {
        "token": "weapons",
        "title": "Weapons",
        "body": "About weapons.",
        "tier": "never",
        "allowed": "Kitchen knives.",
    }
    assert "allowed" not in by_token["electronics"]


# --- the policy is read once per version --------------------------------------------------------


def test_a_second_item_does_not_read_the_policy_again(ctx, rail):
    first = _create(ctx, category="electronics")
    _create(ctx, category="weapons")
    _update(ctx, first["id"], category="home_and_garden")
    dispatch("carousell_ai_item_policy", {}, ctx)
    assert rail.policy_reads == 1


def test_a_token_added_since_the_read_is_found_by_reading_again(ctx, rail):
    _create(ctx, category="electronics")
    rail.item_policy = item_policy("2026-11-01.new", allowed=("electronics", "books"))
    assert _create(ctx, category="books")["category"] == "books"
    assert _create(ctx, category="books")["category"] == "books"
    assert rail.policy_reads == 2


def test_an_unlisted_token_reads_the_policy_again_once_per_version(ctx, rail):
    _create(ctx, category="electronics")
    for _ in range(3):
        with pytest.raises(ToolError):
            _create(ctx, category="crafts")
    assert rail.policy_reads == 2


def test_carousell_ai_calling_a_token_unknown_reads_the_policy_again(ctx, rail):
    _create(ctx, category="weapons")
    rail.item_policy = item_policy("2026-11-01.new", never=("drugs_and_tobacco",))
    with pytest.raises(RailToolRefused, match="is not a category the item policy lists"):
        _factory(rail)().call_tool("create_listing", {"title": "Knife", "category": "weapons"})
    with pytest.raises(ToolError, match="'weapons'"):
        _create(ctx, category="weapons")
    assert rail.policy_reads == 2


# --- properties --------------------------------------------------------------------------------

_TOKENS = ("electronics", "home_and_garden", "books", "too_big_to_ship", "weapons", "crafts")


class ItemCategories(RuleBasedStateMachine):
    """Item writes interleaved with policy changes. The policy is read exactly when the model says
    a read is due, and a write succeeds exactly when the policy as read lists its token."""

    def __init__(self):
        super().__init__()
        rail_client._POLICIES.clear()
        self.tmp = Path(tempfile.mkdtemp())
        data_db = Database(self.tmp / "sellee.db")
        events_db = Database(self.tmp / "events.db")
        migrations.run_startup_migrations(
            data_db=data_db, events_db=events_db, backups_dir=self.tmp / "b", backups_keep=1
        )
        self.dbs = (data_db, events_db)
        self.relay = FakeRelay()
        self.relay.base, self.shutdown = serve(self.relay)
        self.ctx = ToolContext(
            session=Session(tier=TIER_ATTENDED),
            store=Store(data_db),
            bus=EventBus(EventStore(events_db)),
            config=Config(),
            rail_factory=_factory(self.relay),
        )
        self.versions = 0
        self.cached: dict | None = None
        self.missed: set = set()
        self.reads = 0
        self.items: dict = {}

    def teardown(self):
        self.shutdown()
        for db in self.dbs:
            db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _served_tokens(self, policy) -> set:
        return {g["token"] for tier in ("allowed", "not_yet", "never") for g in policy[tier]}

    def _expect_lookup(self, token) -> bool:
        if self.cached is None:
            self._read()
        elif token not in self._served_tokens(self.cached) and token not in self.missed:
            before = self.cached["version"]
            self._read()
            if self.cached["version"] != before:
                self.missed = set()
        if token in self._served_tokens(self.cached):
            return True
        self.missed.add(token)
        return False

    def _read(self):
        self.reads += 1
        self.cached = self.relay.item_policy

    def _write(self, write, token):
        accepted = self._expect_lookup(token)
        try:
            item = write()
        except ToolError:
            assert not accepted, token
            return
        assert accepted, token
        assert item["category"] == token
        self.items[item["id"]] = token

    @rule(token=st.sampled_from(_TOKENS))
    def create(self, token):
        self._write(lambda: _create(self.ctx, category=token), token)

    @precondition(lambda self: self.items)
    @rule(data=st.data(), token=st.sampled_from(_TOKENS))
    def update(self, data, token):
        item_id = data.draw(st.sampled_from(sorted(self.items)))
        self._write(lambda: _update(self.ctx, item_id, category=token), token)

    @rule(tokens=st.sets(st.sampled_from(_TOKENS), min_size=1))
    def carousell_ai_changes_its_policy(self, tokens):
        self.versions += 1
        ordered = sorted(tokens)
        self.relay.item_policy = item_policy(
            f"v{self.versions}", allowed=ordered[::2], not_yet=(), never=ordered[1::2]
        )

    @rule(token=st.sampled_from(_TOKENS))
    def carousell_ai_creates_a_listing(self, token):
        try:
            self.ctx.rail_factory().call_tool("create_listing", {"title": "x", "category": token})
        except RailPolicyRefused:
            pass  # a refusal says nothing about the cached copy
        except RailToolRefused:
            self.cached, self.missed = None, set()

    @invariant()
    def reads_match_the_model(self):
        assert self.relay.policy_reads == self.reads

    @invariant()
    def every_item_keeps_the_token_it_was_given(self):
        for item_id, token in self.items.items():
            assert self.ctx.store.get_item(item_id)["category"] == token


ItemCategories.TestCase.settings = settings(
    max_examples=40,
    stateful_step_count=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
test_item_categories = ItemCategories.TestCase
