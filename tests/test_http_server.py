"""The localhost HTTP server: Host/Origin guard, bearer auth, MCP JSON-RPC, control, web tail."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

import selly_agent.tools  # noqa: F401  registration
from selly_agent.config import Config
from selly_agent.http_server import HttpServer
from selly_agent.paths import PACKAGE_DATA_DIR
from selly_agent.tools.registry import ToolContext


class _FakeRail:
    def create_listing(self, args):
        return {"listing_id": "L1", "url": "https://www.carousell.ai/listing/1"}

    def verify_listing_url(self, url):
        return None


@pytest.fixture
def server(bus, store, xdg_tmp):
    def context_factory(session):
        return ToolContext(
            session=session,
            store=store,
            bus=bus,
            config=Config(),
            rail_factory=lambda: _FakeRail(),
            started_ts=1000.0,
        )

    srv = HttpServer(
        port=0,
        bus=bus,
        store=store,
        events_db_path=bus.store.db.path,
        context_factory=context_factory,
        attended_token="attended-secret",
    )
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def _request(server, method, path, *, token=None, body=None, headers=None):
    url = f"http://127.0.0.1:{server.port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        return exc.code, (json.loads(raw) if raw else None)


def _rpc(server, method, params=None, *, token="attended-secret", msg_id=1):
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
    return _request(server, "POST", "/mcp", token=token, body=body)


# --- hardening --------------------------------------------------------------------------------


def test_bad_host_is_forbidden(server) -> None:
    status, _ = _request(
        server, "POST", "/mcp", token="attended-secret", body={}, headers={"Host": "evil.example"}
    )
    assert status == 403


def test_non_localhost_origin_is_forbidden(server) -> None:
    status, _ = _request(
        server,
        "POST",
        "/mcp",
        token="attended-secret",
        body={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Origin": "http://evil.example"},
    )
    assert status == 403


def test_localhost_origin_is_allowed(server) -> None:
    status, body = _request(
        server,
        "POST",
        "/mcp",
        token="attended-secret",
        body={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Origin": "http://localhost"},
    )
    assert status == 200 and body["result"] == {}


def test_missing_token_is_unauthorized_without_echo(server) -> None:
    status, body = _rpc(server, "ping", token=None)
    assert status == 401
    assert "attended" not in json.dumps(body)  # never echoes the (absent) token


def test_wrong_token_is_unauthorized(server) -> None:
    status, _ = _rpc(server, "ping", token="not-the-token")
    assert status == 401


def test_get_mcp_is_405(server) -> None:
    status, _ = _request(server, "GET", "/mcp", token="attended-secret")
    assert status == 405


# --- MCP protocol -----------------------------------------------------------------------------


def test_initialize_advertises_tools_only(server) -> None:
    status, body = _rpc(server, "initialize", {"protocolVersion": "2025-06-18"})
    assert status == 200
    assert body["result"]["capabilities"] == {"tools": {}}
    assert body["result"]["serverInfo"]["name"] == "selly-agent"


def test_notifications_initialized_returns_202(server) -> None:
    status, body = _request(
        server,
        "POST",
        "/mcp",
        token="attended-secret",
        body={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert status == 202 and body is None


def test_tools_list_filtered_by_tier(server) -> None:
    _, attended = _rpc(server, "tools/list")
    names = {t["name"] for t in attended["result"]["tools"]}
    assert "set_floor" in names and "create_item" in names

    pass_token = server.auth.mint_pass_token("pass:publish", "pass_1", expiry_ts=1e18)
    _, passl = _rpc(server, "tools/list", token=pass_token)
    pass_names = {t["name"] for t in passl["result"]["tools"]}
    assert pass_names == {
        "get_item",
        "carousell_ai_upload_photos",
        "carousell_ai_publish_listing",
        "send_message",
    }


def test_tools_call_round_trip_and_iserror(server, store) -> None:
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    _, ok = _rpc(server, "tools/call", {"name": "get_item", "arguments": {"item_id": item["id"]}})
    assert ok["result"]["isError"] is False
    assert ok["result"]["structuredContent"]["id"] == item["id"]

    _, bad = _rpc(server, "tools/call", {"name": "get_item", "arguments": {"item_id": "nope"}})
    assert bad["result"]["isError"] is True
    assert "no item" in bad["result"]["content"][0]["text"]


def test_tools_call_hidden_tool_is_iserror_not_leak(server) -> None:
    pass_token = server.auth.mint_pass_token("pass:publish", "pass_1", expiry_ts=1e18)
    _, body = _rpc(
        server,
        "tools/call",
        {"name": "set_floor", "arguments": {"item_id": "x", "floor": 5, "source": "seller"}},
        token=pass_token,
    )
    assert body["result"]["isError"] is True
    assert "unknown tool" in body["result"]["content"][0]["text"]


def test_expired_pass_token_is_unauthorized(server) -> None:
    expired = server.auth.mint_pass_token("pass:publish", "pass_1", expiry_ts=1.0)
    status, _ = _rpc(server, "ping", token=expired)
    assert status == 401


def test_parse_error_and_method_not_found(server) -> None:
    status, body = _request(
        server,
        "POST",
        "/mcp",
        token="attended-secret",
        headers={"Content-Type": "application/json"},
        body=None,
    )
    # empty body parses to {} -> invalid request (no jsonrpc field)
    assert status == 200 and body["error"]["code"] == -32600

    _, nf = _rpc(server, "does/not/exist")
    assert nf["error"]["code"] == -32601


# --- control + tail ---------------------------------------------------------------------------


def test_enqueue_pass_creates_row_and_event(server, store, bus) -> None:
    status, body = _request(
        server,
        "POST",
        "/control/enqueue-pass",
        token="attended-secret",
        body={"type": "publish", "payload": {"item_id": "item_1"}},
    )
    assert status == 200
    pass_id = body["pass_id"]
    assert store.get_pass(pass_id)["status"] == "queued"
    queued = [e for e in bus.store.read() if e.kind == "pass.queued"]
    assert queued and queued[0].pass_id == pass_id


def test_enqueue_pass_requires_attended_token(server) -> None:
    pass_token = server.auth.mint_pass_token("pass:publish", "pass_1", expiry_ts=1e18)
    status, _ = _request(
        server, "POST", "/control/enqueue-pass", token=pass_token, body={"type": "publish"}
    )
    assert status == 401


def test_events_json_requires_token(server) -> None:
    status, _ = _request(server, "GET", "/events.json")
    assert status == 401


def test_events_json_returns_events(server, bus) -> None:
    bus.publish("demo.event", {"hello": "world"})
    status, body = _request(server, "GET", "/events.json?token=attended-secret&after_seq=0")
    assert status == 200
    kinds = [e["kind"] for e in body["events"]]
    assert "demo.event" in kinds
    assert body["last_seq"] >= 1
    assert all("@ts" in e for e in body["events"])  # rows share the inspect --json wire shape


def test_tail_serves_the_packaged_page(server) -> None:
    url = f"http://127.0.0.1:{server.port}/tail?token=attended-secret"
    with urllib.request.urlopen(url, timeout=5) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/html")
        served = resp.read()
    assert b"event tail" in served
    # the page is a packaged asset, not an inline string — pin that wiring
    assert served == (PACKAGE_DATA_DIR / "tail.html").read_bytes()
