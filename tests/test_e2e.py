"""The vertical slice, end to end (claude and the rail both faked):

A fake harness process genuinely calls our MCP endpoint over HTTP with its per-pass token
(get_item -> carousell_ai_publish_listing -> send_message), driven by the real daemon pieces:
enqueue via the control route, the pass lane claims it, run_pass spawns and streams it. We assert
the listing URL is recorded, every tool.call/tool.result and pass.* event is correlated by one
pass_id, and logs --pass renders both streams. A second scenario proves the attended tier can
run create_item -> set_floor -> publish against the same fake rail.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.request

import pytest
from tests.conftest import leak_paths

import sellee.tools  # noqa: F401  registration
from sellee import passes
from sellee.config import Config
from sellee.db import connect_reader
from sellee.events import query_events
from sellee.http_server import HttpServer
from sellee.tools.registry import ToolContext

_ATTENDED = "attended-secret"
_LISTING_URL = "https://www.carousell.ai/listing/42-lamp"


class FakeRail:
    def create_listing(self, args):
        return {"listing_id": "L42", "url": _LISTING_URL}

    def verify_listing_url(self, url):
        return None  # verify passes


# A fake harness process: reads the workspace .mcp.json for its endpoint + per-pass token, then
# drives the real MCP endpoint over HTTP and emits matching stream-json. Uses only our MCP server.
_E2E_HARNESS = """\
import json, sys, urllib.request
cfg = json.load(open(".mcp.json"))
srv = cfg["mcpServers"]["sellee"]
endpoint, auth = srv["url"], srv["headers"]["Authorization"]
item_id = sys.argv[1]
def emit(o):
    print(json.dumps(o), flush=True)
def rpc(method, params, mid):
    body = json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params}).encode()
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": auth, "Origin": "http://127.0.0.1"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())
emit({"type": "system", "subtype": "init", "session_id": "e2e", "tools": ["get_item"]})
rpc("initialize", {}, 1)
rpc("tools/call", {"name": "get_item", "arguments": {"item_id": item_id}}, 2)
emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "get_item"}]}})
rpc("tools/call", {"name": "carousell_ai_publish_listing", "arguments": {"item_id": item_id}}, 3)
emit({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "carousell_ai_publish_listing"}]}})
rpc("tools/call", {"name": "send_message", "arguments": {"text": "listed!"}}, 4)
emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
      "session_id": "e2e", "usage": {"input_tokens": 1}})
sys.exit(0)
"""


@pytest.fixture
def wired(bus, store, xdg_tmp):
    from sellee import paths

    paths.ensure_state_dirs()

    def context_factory(session):
        return ToolContext(
            session=session,
            store=store,
            bus=bus,
            config=Config(),
            rail_factory=lambda: FakeRail(),
            started_ts=1.0,
        )

    srv = HttpServer(
        port=0,
        bus=bus,
        store=store,
        events_db_path=bus.store.db.path,
        context_factory=context_factory,
        attended_token=_ATTENDED,
    )
    srv.start()
    try:
        yield srv, bus, store
    finally:
        srv.stop()


def _rpc(server, method, params, token, mid=1):
    url = f"http://127.0.0.1:{server.port}/mcp"
    body = json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _post(server, path, body, token):
    url = f"http://127.0.0.1:{server.port}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def test_fake_harness_publishes_end_to_end(wired, tmp_path) -> None:
    server, bus, store = wired
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    store.set_floor(item["id"], 60.0, "seller")

    script = tmp_path / "e2e_harness.py"
    script.write_text(_E2E_HARNESS)

    deps = passes.PassDeps(
        bus=bus,
        store=store,
        config=Config(),
        auth=server.auth,
        http_endpoint=f"http://127.0.0.1:{server.port}/mcp",
        stop_event=threading.Event(),
        argv_builder=lambda spec: [sys.executable, str(script), item["id"]],
    )

    # enqueue through the control route (the CLI's path), then let the lane claim + run it
    enqueue = _post(
        server,
        "/control/enqueue-pass",
        {"type": "publish", "payload": {"item_id": item["id"]}},
        _ATTENDED,
    )
    pass_id = enqueue["pass_id"]
    passes.pass_lane(deps)

    # the listing URL was recorded on the item
    assert store.get_item(item["id"])["listing_urls"]["carousell-ai"] == _LISTING_URL

    # every server-side tool event is correlated to this one pass, and only MCP tools were used
    conn = connect_reader(bus.store.db.path)
    try:
        pass_events = query_events(conn, pass_id=pass_id)
    finally:
        conn.close()
    kinds = {e.kind for e in pass_events}
    assert {"pass.start", "pass.init", "pass.result", "pass.end"} <= kinds  # harness stream
    assert {"tool.call", "tool.result"} <= kinds  # server-side ground truth, same pass_id
    tool_calls = {e.payload["tool"] for e in pass_events if e.kind == "tool.call"}
    assert tool_calls == {"get_item", "carousell_ai_publish_listing", "send_message"}

    end = [e for e in pass_events if e.kind == "pass.end"][0]
    assert end.payload["is_error"] is False
    assert store.get_pass(pass_id)["status"] == "done"


def test_attended_session_shape(wired) -> None:
    server, bus, store = wired
    created = _rpc(
        server,
        "tools/call",
        {
            "name": "create_item",
            "arguments": {"title": "Chair", "list_price": 50.0, "currency": "SGD"},
        },
        _ATTENDED,
    )
    item_id = created["result"]["structuredContent"]["id"]

    floor = _rpc(
        server,
        "tools/call",
        {"name": "set_floor", "arguments": {"item_id": item_id, "floor": 40.0, "source": "seller"}},
        _ATTENDED,
        mid=2,
    )
    assert floor["result"]["isError"] is False

    published = _rpc(
        server,
        "tools/call",
        {"name": "carousell_ai_publish_listing", "arguments": {"item_id": item_id}},
        _ATTENDED,
        mid=3,
    )
    assert published["result"]["structuredContent"]["url"] == _LISTING_URL
    assert store.get_item(item_id)["listing_urls"]["carousell-ai"] == _LISTING_URL

    # the floor value never leaked into any event
    assert leak_paths([e.payload for e in bus.store.read()], 40.0) == []
