"""Entity scoping end to end over the real MCP endpoint: a scoped pass token minted at spawn is
held to its scope by the ScopedStore the context factory builds — out-of-scope reads answer
exactly like a missing row, and tools/list is unaffected by scope."""

from __future__ import annotations

import json
import urllib.request

import pytest

import selly_agent.tools  # noqa: F401  registration
from selly_agent.config import Config
from selly_agent.http_server import HttpServer
from selly_agent.store import Scope, ScopedStore
from selly_agent.tools.registry import TIER_PASS_REPLY, ToolContext

_ATTENDED = "attended-secret"


@pytest.fixture
def scoped_server(bus, store, xdg_tmp):
    from selly_agent import paths

    paths.ensure_state_dirs()

    def context_factory(session):
        # exactly what the daemon builds: a ScopedStore bound to the session's scope
        return ToolContext(
            session=session,
            store=ScopedStore(store, getattr(session, "scope", None)),
            bus=bus,
            config=Config(),
            rail_factory=None,
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
        yield srv, store
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


def test_scoped_token_sees_only_its_spawn_scope(scoped_server) -> None:
    server, store = scoped_server
    i1 = store.create_item(title="A", list_price=80.0, currency="SGD")
    i2 = store.create_item(title="B", list_price=90.0, currency="SGD")
    store.create_thread(
        thread_id="fb:1", side="sell", market="fb", counterpart_handle="a", item_id=i1["id"]
    )
    store.create_thread(
        thread_id="fb:2", side="sell", market="fb", counterpart_handle="b", item_id=i2["id"]
    )

    scope = Scope.of(threads={"fb:1"}, items={i1["id"]})
    token = server.auth.mint_pass_token(TIER_PASS_REPLY, "pass_1", expiry_ts=1e18, scope=scope)

    # in scope: real rows
    in_item = _rpc(
        server, "tools/call", {"name": "get_item", "arguments": {"item_id": i1["id"]}}, token
    )
    assert in_item["result"]["isError"] is False
    in_thread = _rpc(
        server,
        "tools/call",
        {"name": "get_thread", "arguments": {"thread_id": "fb:1"}},
        token,
        mid=2,
    )
    assert in_thread["result"]["isError"] is False

    # out of scope: the same not-found a missing row gets — never a distinguishable answer
    out_item = _rpc(
        server, "tools/call", {"name": "get_item", "arguments": {"item_id": i2["id"]}}, token, mid=3
    )
    assert (
        out_item["result"]["isError"] is True
        and i2["id"] in out_item["result"]["content"][0]["text"]
    )
    out_thread = _rpc(
        server,
        "tools/call",
        {"name": "get_thread", "arguments": {"thread_id": "fb:2"}},
        token,
        mid=4,
    )
    assert out_thread["result"]["isError"] is True

    # tools/list is unaffected by scope (tier filtering only)
    listed = _rpc(server, "tools/list", {}, token, mid=5)
    names = {t["name"] for t in listed["result"]["tools"]}
    assert "get_item" in names and "get_thread" in names
