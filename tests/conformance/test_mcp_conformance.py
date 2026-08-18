"""Drive our hand-rolled MCP server with the official MCP SDK as client — the interop proof.

stdlib-only constrains the user *runtime*, not dev/CI deps: the `mcp` SDK is a dev-only
dependency used here purely as a conformance client. The SDK needs Python 3.10+, so this whole
module skips on the 3.9 runtime floor with an honest message — `make test-3.9` stays green and
truthful about the skip, and the interop is still proven on the dev/CI interpreter.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp", reason="the MCP SDK is a dev-only conformance client")

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

import sellee.tools  # noqa: E402,F401  registration
from sellee.config import Config  # noqa: E402
from sellee.http_server import HttpServer  # noqa: E402
from sellee.tools.registry import ToolContext  # noqa: E402

_TOKEN = "attended-secret"


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
        attended_token=_TOKEN,
    )
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def _url(server) -> str:
    return f"http://127.0.0.1:{server.port}/mcp"


def test_sdk_initialize_list_and_call(server, store) -> None:
    item = store.create_item(title="Lamp", list_price=80.0, currency="SGD")

    async def scenario():
        headers = {"Authorization": f"Bearer {_TOKEN}"}
        async with streamablehttp_client(_url(server), headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.capabilities.tools is not None

                listed = await session.list_tools()
                names = {t.name for t in listed.tools}
                assert "get_item" in names and "carousell_ai_publish_listing" in names

                result = await session.call_tool("get_item", {"item_id": item["id"]})
                assert result.isError is False
                text = result.content[0].text
                assert json.loads(text)["id"] == item["id"]

                # a tool-level failure comes back as an isError result, not a transport error
                bad = await session.call_tool("get_item", {"item_id": "nope"})
                assert bad.isError is True

                await session.send_ping()

    asyncio.run(scenario())


def test_sdk_bad_token_is_rejected(server) -> None:
    async def scenario():
        headers = {"Authorization": "Bearer wrong-token"}
        async with streamablehttp_client(_url(server), headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

    with pytest.raises(Exception):  # noqa: B017 — any transport/protocol failure is acceptable
        asyncio.run(scenario())
