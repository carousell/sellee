"""The fan-out, end to end (claude, the rail and the Bot API all faked):

The channel flow lists an item on carousell.ai and the seller approves `connected_markets`
through the settings door. The lane notices the item is missing from Carousell and queues a
browser publish; a fake harness process drives the real MCP endpoint with its per-pass token and
records the live URL the way a real publish reports its result; the next tick reports it, and the
success notice reaches the bound chat. Then the failure branch: a pass that dies settles as
error, the seller gets one needs-me notice naming the CLI retry, and the pair is never queued
again.
"""

from __future__ import annotations

import sys
import threading
import time as _time

import pytest

import sellee.tools  # noqa: F401  registration
from fake_telegram_api import CHAT_ID, FAKE_TOKEN, FakeTelegramAPI
from sellee import crosslist, passes, secrets, settings
from sellee.channel import fastpaths, outbound
from sellee.channel.telegram.transport import TelegramClient
from sellee.config import Config
from sellee.http_server import HttpServer
from sellee.rail.client import RailUnprovisioned
from sellee.store import ScopedStore
from sellee.tools.registry import TIER_PASS_CHANNEL, ToolContext, dispatch

_ATTENDED = "attended-secret"
_RAIL_URL = "https://www.carousell.ai/listing/teak-lamp-1"
_MARKET_URL = "https://www.carousell.sg/p/teak-lamp-1328307791"


class FakeRail:
    def create_listing(self, args):
        return {"listing_id": "L1", "url": _RAIL_URL}

    def verify_listing_url(self, url):
        return None  # verify passes


# A fake publish pass: reads its workspace .mcp.json for the endpoint and per-pass token, then
# records the live listing URL over the real MCP surface — the way a browser publish's result
# gets back at all.
_PUBLISH_HARNESS = """\
import json, sys, urllib.request
cfg = json.load(open(".mcp.json"))
srv = cfg["mcpServers"]["sellee"]
endpoint, auth = srv["url"], srv["headers"]["Authorization"]
item_id, url = sys.argv[1], sys.argv[2]
def emit(o):
    print(json.dumps(o), flush=True)
def rpc(method, params, mid):
    body = json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params}).encode()
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": auth, "Origin": "http://127.0.0.1"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())
emit({"type": "system", "subtype": "init", "session_id": "xl-e2e", "tools": ["get_item"]})
rpc("initialize", {}, 1)
rpc("tools/call", {"name": "get_item", "arguments": {"item_id": item_id}}, 2)
rpc("tools/call", {"name": "record_published_listing_url",
    "arguments": {"item_id": item_id, "market": "carousell", "url": url}}, 3)
emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
      "session_id": "xl-e2e", "usage": {"input_tokens": 1}})
sys.exit(0)
"""

# The failure branch: a pass that dies having recorded nothing.
_DOOMED_HARNESS = """\
import json, sys
print(json.dumps({"type": "system", "subtype": "init", "session_id": "xl-e2e", "tools": []}),
      flush=True)
sys.exit(1)
"""


@pytest.fixture
def wired(bus, store, xdg_tmp):
    from sellee import paths

    paths.ensure_state_dirs()

    def context_factory(session):
        return ToolContext(
            session=session,
            store=ScopedStore(store, getattr(session, "scope", None)),
            bus=bus,
            config=Config(),
            rail_factory=lambda: FakeRail(),
            started_ts=1.0,
        )

    server = HttpServer(
        port=0,
        bus=bus,
        store=store,
        events_db_path=bus.store.db.path,
        context_factory=context_factory,
        attended_token=_ATTENDED,
    )
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _noon() -> float:
    stamp = _time.localtime()
    return _time.mktime(
        (stamp.tm_year, stamp.tm_mon, stamp.tm_mday, 12, 0, 0, stamp.tm_wday, stamp.tm_yday, -1)
    )


def _no_rail():
    raise RailUnprovisioned("carousell.ai is not provisioned")


def _lane(store, bus) -> None:
    # Real time: the retry cooldown compares this clock against the store-written `finished_ts`,
    # so a fixed fake hour turns it off.
    crosslist.crosslist_lane(
        crosslist.CrosslistDeps(
            store=store,
            bus=bus,
            config=Config(),
            browser_factory=lambda: object(),
            rail_factory=_no_rail,
        )
    )


def _pass_deps(server, bus, store, script, *argv):
    return passes.PassDeps(
        bus=bus,
        store=store,
        config=Config(),
        auth=server.auth,
        http_endpoint=f"http://127.0.0.1:{server.port}/mcp",
        stop_event=threading.Event(),
        argv_builder=lambda spec: [sys.executable, str(script), *argv],
    )


def _bind(store):
    secrets.write_telegram_bot_token(FAKE_TOKEN)
    store.arm_bind("sellee_test_bot", "n1")
    store.complete_bind(CHAT_ID, update_offset=1, nonce=store.get_channel()["bind_nonce"])


def _drain(store, bus, api) -> list:
    def deliver(chat_id, text, controls=None):
        TelegramClient(FAKE_TOKEN, api_base=api.base_url).send_message(chat_id, text)

    outbound.drain_notices(store=store, bus=bus, deliver=deliver)
    return [m["text"] for m in api.outbox]


def _queued(store):
    # Projected: `finished_ts` is the retry clock's business, not these tests'.
    return [
        {k: v for k, v in row.items() if k != "finished_ts"}
        for row in store.publish_pass_index()
        if row["status"] == "queued"
    ]


def test_the_fan_out_end_to_end(wired, bus, store, make_ctx, tmp_path, xdg_tmp) -> None:
    server = wired
    store.set_seller_config_section("basics", {"region": "SG"})
    _bind(store)

    # The channel flow lists the item on the rail, in conversation.
    ctx = make_ctx(TIER_PASS_CHANNEL, pass_id="p_chan", rail_factory=FakeRail)
    item = dispatch(
        "create_item", {"title": "Teak lamp", "list_price": 80.0, "currency": "SGD"}, ctx
    )
    dispatch("carousell_ai_publish_listing", {"item_id": item["id"]}, ctx)
    assert store.get_item(item["id"])["listing_urls"]["carousell-ai"] == _RAIL_URL

    # The seller asks for Carousell; the approval ask reaches the phone; the door applies it.
    out = dispatch(
        "propose_setting_change", {"key": "connected_markets", "raw_value": ["carousell"]}, ctx
    )
    assert out["status"] == "held"
    with FakeTelegramAPI() as api:
        asks = _drain(store, bus, api)
    assert any("Carousell" in text for text in asks)
    fastpaths.handle_settings_door(
        store,
        bus,
        {"kind": "action", "payload": {"choice": settings.CB_APPROVE, "ref": out["change_id"]}},
    )
    assert settings.publish_markets(store) == ["carousell"]

    # Before listing anywhere new the lane looks at what the seller already has there; here it
    # finds nothing.
    store.record_survey_result("carousell", [])

    # The lane notices the gap and queues the browser publish.
    _lane(store, bus)
    assert _queued(store) == [
        {"market": "carousell", "item_id": item["id"], "origin": "crosslist", "status": "queued"}
    ]

    # A scripted pass publishes and records the live URL through the real MCP surface.
    script = tmp_path / "publish_harness.py"
    script.write_text(_PUBLISH_HARNESS)
    passes.pass_lane(_pass_deps(server, bus, store, script, item["id"], _MARKET_URL))
    assert store.get_item(item["id"])["listing_urls"]["carousell"] == _MARKET_URL

    # The next tick reports it, and the success notice reaches the bound chat.
    _lane(store, bus)
    with FakeTelegramAPI() as api:
        texts = _drain(store, bus, api)
    assert texts == [f"Teak lamp is now listed on Carousell: {_MARKET_URL}"]

    # The pair is settled: nothing further is ever queued for it.
    _lane(store, bus)
    assert _queued(store) == []


def test_a_failed_fan_out_is_one_notice_and_no_second_attempt(
    wired, bus, store, tmp_path, xdg_tmp
) -> None:
    from tests.conftest import seed_setting

    server = wired
    store.set_seller_config_section("basics", {"region": "SG"})
    _bind(store)
    seed_setting(store, "connected_markets", ["carousell"])
    item = store.create_item(title="Teak lamp", list_price=80.0, currency="SGD")
    store.record_listing_url(item["id"], "carousell-ai", _RAIL_URL)
    store.record_survey_result("carousell", [])  # looked first, and the seller had nothing there

    _lane(store, bus)
    assert len(_queued(store)) == 1
    script = tmp_path / "doomed_harness.py"
    script.write_text(_DOOMED_HARNESS)
    passes.pass_lane(_pass_deps(server, bus, store, script))
    assert store.publish_pass_index()[0]["status"] == "error"

    # Nothing is said yet: another go is coming, and repeated "couldn't list" notices train the
    # seller to ignore them. The cooldown holds the retry too.
    _lane(store, bus)
    with FakeTelegramAPI() as api:
        assert _drain(store, bus, api) == []
    assert _queued(store) == []

    # Run the attempts out. Only the last one speaks.
    for _ in range(crosslist.PUBLISH_MAX_ATTEMPTS - 1):
        store.record_driven_publish(
            item["id"], "carousell", status="error", origin=crosslist.ORIGIN
        )
    _lane(store, bus)
    with FakeTelegramAPI() as api:
        texts = _drain(store, bus, api)
    assert len(texts) == 1
    assert "couldn't list Teak lamp on Carousell" in texts[0]
    assert "Ask me" in texts[0]
    assert "carousell.ai listing" in texts[0]
    assert _queued(store) == []
