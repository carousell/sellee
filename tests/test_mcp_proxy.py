"""mcp-proxy: a stdio round-trip forwarding newline-delimited JSON-RPC to a live server."""

from __future__ import annotations

import io
import json

import pytest

import sellee.tools  # noqa: F401  registration
from sellee import mcp_proxy
from sellee.config import Config
from sellee.http_server import HttpServer
from sellee.tools.registry import ToolContext


@pytest.fixture
def server(bus, store, xdg_tmp):
    def context_factory(session):
        return ToolContext(session=session, store=store, bus=bus, config=Config(), started_ts=1.0)

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


def _endpoint(server):
    return f"http://127.0.0.1:{server.port}/mcp"


def test_forward_request_returns_response(server) -> None:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    out = mcp_proxy.forward(body, _endpoint(server), "attended-secret")
    assert json.loads(out)["result"] == {}


def test_forward_notification_yields_no_line(server) -> None:
    body = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
    assert mcp_proxy.forward(body, _endpoint(server), "attended-secret") is None


def test_forward_transport_failure_becomes_rpc_error() -> None:
    body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}).encode()
    out = mcp_proxy.forward(body, "http://127.0.0.1:1/mcp", "t", timeout=1)
    err = json.loads(out)
    assert err["id"] == 7 and err["error"]["code"] == -32603


def test_run_loop_round_trips_stdio(server, store) -> None:
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_item", "arguments": {"item_id": item["id"]}},
        },
    ]
    stdin = io.StringIO("\n".join(json.dumps(m) for m in lines) + "\n")
    stdout = io.StringIO()
    rc = mcp_proxy.run_loop(stdin, stdout, _endpoint(server), "attended-secret")
    assert rc == 0

    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    # two requests -> two replies; the notification produced no line
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[1]["result"]["structuredContent"]["id"] == item["id"]
