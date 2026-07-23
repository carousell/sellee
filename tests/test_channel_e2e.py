"""The channel vertical slice, end to end (fake Bot API + fake harness + fake rail):

* unbound — an escalation queues a notice, catchup surfaces + stamps it, send_message queues;
* bind + conversation — connect over the control route, /start binds, an inbound text routes a
  channel pass that (via a fake harness driving the REAL MCP endpoint) creates an item, sets a
  floor, and replies with send_message; the reply drains to the fake API; a second turn sees the
  first in its transcript window; every server-side tool event is pass_id-correlated; and the
  planted bot token appears in no event payload;
* pause under load — /pause lands while a channel pass is queued, acked instantly, and the pass
  lane claims nothing until resume.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.request

from fake_telegram_api import CHAT_ID, FAKE_TOKEN, FakeTelegramAPI
from selly_agent import passes, paths, secrets
from selly_agent.channel import outbound
from selly_agent.channel.telegram.poller import Poller
from selly_agent.channel.telegram.transport import TelegramClient
from selly_agent.config import Config
from selly_agent.http_server import HttpServer
from selly_agent.store import ScopedStore
from selly_agent.tools import TIER_ATTENDED
from selly_agent.tools.registry import Session, ToolContext, dispatch

_ATTENDED = "attended-secret"
_LISTING_URL = "https://www.carousell.ai/listing/lamp"


class FakeRail:
    def create_listing(self, args):
        return {"listing_id": "L1", "url": _LISTING_URL}

    def verify_listing_url(self, url):
        return None


# A fake channel-pass harness: reads its per-pass token from the workspace .mcp.json, then drives
# the real MCP endpoint — create_item -> set_floor -> send_message — as the pass:channel tier.
_CHANNEL_HARNESS = """\
import json, sys, urllib.request
cfg = json.load(open(".mcp.json"))
srv = cfg["mcpServers"]["selly"]
endpoint, auth = srv["url"], srv["headers"]["Authorization"]
def emit(o): print(json.dumps(o), flush=True)
def rpc(method, params, mid):
    body = json.dumps({"jsonrpc":"2.0","id":mid,"method":method,"params":params}).encode()
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Content-Type":"application/json","Authorization":auth,"Origin":"http://127.0.0.1"})
    with urllib.request.urlopen(req, timeout=10) as r: return json.loads(r.read().decode())
emit({"type":"system","subtype":"init","session_id":"chan"})
rpc("initialize", {}, 1)
created = rpc("tools/call", {"name":"create_item",
    "arguments":{"title":"Lamp","list_price":80.0,"currency":"SGD"}}, 2)
item_id = created["result"]["structuredContent"]["id"]
rpc("tools/call", {"name":"set_floor",
    "arguments":{"item_id":item_id,"floor":60.0,"source":"seller"}}, 3)
rpc("tools/call", {"name":"send_message", "arguments":{"text":"Listed your lamp at $80."}}, 4)
emit({"type":"result","subtype":"success","is_error":False,"num_turns":3,
      "session_id":"chan","usage":{"input_tokens":1}})
sys.exit(0)
"""


def _post(server, path, body, token=_ATTENDED):
    url = f"http://127.0.0.1:{server.port}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _deliver(api):
    def deliver(chat_id, text):
        TelegramClient(FAKE_TOKEN, api_base=api.base_url).send_message(chat_id, text)

    return deliver


def _poller(store, bus, api, cfg):
    return Poller(
        store=store,
        config=cfg,
        bus=bus,
        stop_event=threading.Event(),
        client_factory=lambda token: TelegramClient(FAKE_TOKEN, api_base=api.base_url),
        poll_timeout=0,
    )


def _server(bus, store, cfg):
    def context_factory(session):
        return ToolContext(
            session=session,
            store=ScopedStore(store, getattr(session, "scope", None)),
            bus=bus,
            config=cfg,
            rail_factory=lambda: FakeRail(),
            started_ts=1.0,
        )

    return HttpServer(
        port=0,
        bus=bus,
        store=store,
        events_db_path=bus.store.db.path,
        context_factory=context_factory,
        attended_token=_ATTENDED,
        config=cfg,
    )


# --- unbound --------------------------------------------------------------------------------


def test_unbound_escalation_surfaces_and_send_queues(bus, store, xdg_tmp) -> None:
    bus.subscribe(outbound.escalation_notifier(store))
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    store.create_thread(
        thread_id="carousell:t1",
        side="sell",
        market="carousell",
        counterpart_handle="b",
        item_id=item["id"],
    )

    def ctx():
        return ToolContext(
            session=Session(tier=TIER_ATTENDED), store=store, bus=bus, config=Config()
        )

    dispatch("escalate", {"thread_id": "carousell:t1", "open_question": "Accept $70?"}, ctx())
    dispatch("send_message", {"text": "fyi"}, ctx())  # unbound: still queues

    catchup = dispatch("get_catchup", {}, ctx())
    assert catchup["counts"]["escalations"] == 1
    assert catchup["counts"]["notices"] == 2  # the escalation-push notice + the send_message one
    assert catchup["channel"]["bound"] is False
    # catchup delivered (stamped) the notices; the escalation stays open
    assert store.count_queued_notices() == 0
    assert dispatch("get_status", {}, ctx())["channel_bound"] is False


# --- bind + conversation + leak sweep -------------------------------------------------------


def test_bind_then_conversation_end_to_end(bus, store, xdg_tmp, tmp_path) -> None:
    paths.ensure_state_dirs()
    bus.subscribe(outbound.channel_pass_folder(store))
    with FakeTelegramAPI() as api:
        cfg = Config(quiet_hours=(0, 0), reply_delay_sec=(0, 0), telegram_api_base=api.base_url)
        server = _server(bus, store, cfg)
        server.start()
        try:
            # bind: connect over the control route, then a /start carrying the nonce
            body = _post(server, "/control/connect-telegram", {"token": FAKE_TOKEN})
            nonce = body["start_url"].split("start=", 1)[1]
            api.inject_command("/start " + nonce)
            poller = _poller(store, bus, api, cfg)
            poller.tick()
            assert store.get_channel()["chat_id"] == CHAT_ID

            # turn 1: an inbound text routes a channel pass
            api.inject_text("sell my lamp for 80")
            poller.tick()
            script = tmp_path / "chan.py"
            script.write_text(_CHANNEL_HARNESS)
            deps = passes.PassDeps(
                bus=bus,
                store=store,
                config=cfg,
                auth=server.auth,
                http_endpoint=f"http://127.0.0.1:{server.port}/mcp",
                stop_event=threading.Event(),
                argv_builder=lambda spec: [sys.executable, str(script)],
            )
            passes.pass_lane(deps)  # claims + runs the channel pass
            outbound.drain_notices(store=store, bus=bus, deliver=_deliver(api))

            # the pass acted and its reply reached the phone
            assert any(i["title"] == "Lamp" for i in store.list_items())
            assert any("Listed your lamp at $80." == m["text"] for m in api.outbox)

            pass_id = next(e.pass_id for e in bus.store.read() if e.kind == "pass.queued")
            assert all(r["status"] == "handled" for r in store.inbox_for_pass(pass_id))
            # every server-side tool call is correlated to the one pass
            tool_calls = {
                e.payload["tool"]
                for e in bus.store.read()
                if e.kind == "tool.call" and e.pass_id == pass_id
            }
            assert {"create_item", "set_floor", "send_message"} <= tool_calls

            # turn 2 sees turn 1 (the message and the agent's reply) in its transcript window
            api.inject_text("thanks!")
            poller.tick()
            pass_id2 = [e for e in bus.store.read() if e.kind == "pass.queued"][-1].pass_id
            prompt = passes._channel_prompt({}, store, pass_id2)
            assert "sell my lamp for 80" in prompt and "Listed your lamp at $80." in prompt

            # leak sweep: the bot token appears in no recorded event payload
            for event in bus.store.read():
                assert FAKE_TOKEN not in json.dumps(event.payload)
        finally:
            server.stop()


# --- pause under load -----------------------------------------------------------------------


def test_pause_fastpath_while_a_channel_pass_is_queued(bus, store, xdg_tmp) -> None:
    secrets.write_telegram_bot_token(FAKE_TOKEN)
    store.arm_bind("selly_test_bot", "n1")
    store.complete_bind(CHAT_ID, update_offset=1)
    with FakeTelegramAPI() as api:
        cfg = Config(telegram_api_base=api.base_url)
        poller = _poller(store, bus, api, cfg)
        api.inject_text("do something")
        poller.tick()  # routes a channel pass (queued)
        assert store.has_active_channel_pass() is True
        api.inject_command("/pause")
        poller.tick()  # fast path: instant ack + pause flag, no LLM
        assert store.is_paused() is True
        assert any("Paused" in m["text"] for m in api.outbox)

    # the pass lane claims nothing while paused (argv would fail loudly if a pass ever ran)
    deps = passes.PassDeps(
        bus=bus,
        store=store,
        config=Config(),
        auth=_FakeAuth(),
        http_endpoint="http://127.0.0.1:1/mcp",
        stop_event=threading.Event(),
        argv_builder=lambda spec: ["/bin/false"],
    )
    passes.pass_lane(deps)
    assert store.has_active_channel_pass() is True  # still queued, never claimed


class _FakeAuth:
    def mint_pass_token(self, tier, pass_id, expiry_ts):
        return "tok"

    def revoke_pass_token(self, token):
        pass
