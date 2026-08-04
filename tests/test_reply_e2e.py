"""The reply loop end to end, with the harness, the rail and the browser all faked.

A buyer message is read off a fake marketplace into durable rows, the lane spawns a scoped reply
pass, a fake harness process genuinely calls our MCP endpoint over HTTP with that pass's token
(negotiating an offer, then sending), and the send goes out through the browser sink. The properties
worth an end-to-end test are the ones no unit can show: that the scope minted into the token really
stops the pass reading another buyer's thread, and that the floor never appears anywhere along the
way — not in a tool result, not in an event, not in the reply.
"""

from __future__ import annotations

import json
import sys
import threading

import pytest
from tests.conftest import enable_markets, leak_paths

import selly_agent.tools  # noqa: F401  registration
from selly_agent import passes
from selly_agent.browser import inbox
from selly_agent.config import Config
from selly_agent.db import connect_reader
from selly_agent.events import query_events
from selly_agent.http_server import HttpServer
from selly_agent.store import ScopedStore
from selly_agent.tools.registry import ToolContext

_ATTENDED = "attended-secret"
_FLOOR = 61.0  # the sentinel: this number must never leave the daemon
_LIST_PRICE = 80.0

# A fake harness: reads its own workspace config for the endpoint and per-pass token, then drives
# the real MCP endpoint. It negotiates the buyer's offer and sends what the engine decided, so the
# scope, the pacing gate, the sink and the commit are all the real ones.
_REPLY_HARNESS = """\
import json, sys, urllib.request
cfg = json.load(open(".mcp.json"))
srv = cfg["mcpServers"]["selly"]
endpoint, auth = srv["url"], srv["headers"]["Authorization"]
thread_id, item_id, other_thread = sys.argv[1], sys.argv[2], sys.argv[3]
def emit(o):
    print(json.dumps(o), flush=True)
def rpc(method, params, mid):
    body = json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params}).encode()
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": auth, "Origin": "http://127.0.0.1"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())
emit({"type": "system", "subtype": "init", "session_id": "reply-e2e", "tools": ["get_thread"]})
rpc("initialize", {}, 1)
rpc("tools/call", {"name": "get_thread", "arguments": {"thread_id": thread_id}}, 2)
# reaching for a thread this pass was not spawned for must read as absent
peek = rpc("tools/call", {"name": "get_thread", "arguments": {"thread_id": other_thread}}, 3)
open("peek.json", "w").write(json.dumps(peek))
decided = rpc("tools/call", {"name": "negotiate_offer", "arguments": {
    "item_id": item_id, "thread_id": thread_id, "buyer": "bob", "offer": 70}}, 4)
open("decided.json", "w").write(json.dumps(decided))
result = decided.get("result", {}).get("structuredContent", {})
counter = result.get("counter_price")
text = "I could do %s, deal?" % counter if counter else "let me check and come back to you"
rpc("tools/call", {"name": "send_reply", "arguments": {
    "thread_id": thread_id, "text": text, "kind": "reply", "in_msg_id": "in|m1"}}, 5)
emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 4,
      "session_id": "reply-e2e", "usage": {"input_tokens": 1}})
sys.exit(0)
"""


class RecordingSink:
    """Stands in for the browser sink: records the send and stamps the intent as the real one."""

    def __init__(self, store):
        self.store = store
        self.sends: list = []

    def send(self, thread, text, kind, intent_id):
        self.sends.append({"thread_id": thread["thread_id"], "text": text, "kind": kind})
        self.store.mark_intent_sent_unverified(intent_id)


@pytest.fixture
def wired(bus, store, xdg_tmp):
    from selly_agent import paths

    paths.ensure_state_dirs()
    enable_markets(store, "carousell")  # the lane claims only for markets the seller enabled
    sink = RecordingSink(store)

    def context_factory(session):
        return ToolContext(
            session=session,
            store=ScopedStore(store, getattr(session, "scope", None)),
            bus=bus,
            config=Config(reply_delay_sec=(0, 0), interactive_reply_delay_sec=(0, 0)),
            reply_sink=lambda: sink,
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
        yield server, bus, store, sink
    finally:
        server.stop()


class StubBrowser:
    """A marketplace with one waiting buyer, answering the adapter's artifacts."""

    def __init__(self, text):
        self.text = text
        self.url = ""

    class _Held:
        def __init__(self, client):
            self.client = client

        def __enter__(self):
            return self.client

        def __exit__(self, *exc):
            return False

    def exclusive(self):
        return self._Held(self)

    def navigate(self, url):
        self.url = url

    def evaluate(self, function, **kwargs):
        from selly_agent.browser.markets import carousell as market

        if function == market.LOGIN_JS:
            return {"state": "logged_in"}
        if function == market.CONVERSATIONS_LIST_JS:
            return {
                "conversations": [
                    {
                        "thread_id": "1",
                        "handle": "bob",
                        "product_id": "1",
                        "unread": 1,
                        "last_message": self.text,
                        "offer_type": "received",
                    }
                ]
            }
        if function == market.CONVERSATION_TAIL_JS:
            return [{"text": self.text, "side": "in", "y": 1}]
        raise AssertionError("unexpected evaluate")


def _seed(store):
    """A published item with a private floor, and a second buyer's thread the pass must not see."""
    store.set_seller_config_section("basics", {"region": "SG"})
    item = store.create_item(title="Teak lamp", list_price=_LIST_PRICE, currency="SGD")
    store.set_floor(item["id"], _FLOOR, "seller")
    store.record_listing_url(item["id"], "carousell", "https://www.carousell.sg/p/lamp-1/")

    # A second buyer, already answered — so the lane has no reason to claim them into this pass, and
    # their conversation is genuinely outside the scope it mints.
    other_item = store.create_item(title="Office chair", list_price=40.0, currency="SGD")
    store.create_thread(
        thread_id="carousell:999",
        side="sell",
        market="carousell",
        counterpart_handle="carol",
        item_id=other_item["id"],
    )
    store.record_inbound("carousell:999", msg_id="x", text="another buyer's words", ts=5.0)
    store.record_inbound(
        "carousell:999", msg_id="y", text="already sorted", ts=6.0, direction="out"
    )
    return item


def test_a_buyer_message_becomes_a_scoped_reply_that_reaches_the_marketplace(
    wired, tmp_path
) -> None:
    server, bus, store, sink = wired
    item = _seed(store)

    # 1. the read lane finds the buyer and writes the row — no model involved
    browser = StubBrowser("can you do 70?")
    inbox.inbox_lane(
        inbox.InboxDeps(
            store=store,
            bus=bus,
            config=Config(),
            browser_factory=lambda: browser,
            now=lambda: 100.0,
        )
    )
    thread = store.get_thread("carousell:1")
    assert [m["text"] for m in thread["messages"]] == ["can you do 70?"]
    assert thread["messages"][0]["scam_verdict"] == "clean"

    # 2. the reply lane claims it into one scoped pass
    inbox.reply_lane(store=store, bus=bus)
    claimed = store.claim_queued_pass()
    assert claimed.type == "reply"
    assert claimed.payload["thread_ids"] == ["carousell:1"]

    # 3. the pass runs for real: its token carries the scope, and it drives our MCP endpoint
    script = tmp_path / "reply_harness.py"
    script.write_text(_REPLY_HARNESS)
    workspace_holder: dict = {}

    def argv_builder(spec):
        workspace_holder["spec"] = spec
        return [sys.executable, str(script), "carousell:1", item["id"], "carousell:999"]

    deps = passes.PassDeps(
        bus=bus,
        store=store,
        config=Config(reply_delay_sec=(0, 0)),
        auth=server.auth,
        http_endpoint=f"http://127.0.0.1:{server.port}/mcp",
        stop_event=threading.Event(),
        argv_builder=argv_builder,
    )
    outcome = passes.run_pass(deps, claimed)
    assert outcome == "ok"

    # the reply went out through the sink, and the engine's counter is what it said
    assert len(sink.sends) == 1
    sent = sink.sends[0]
    assert sent["thread_id"] == "carousell:1"
    assert "let me check" not in sent["text"]  # the engine decided a real counter

    # the bracket completed: an outbound row, the cursor advanced, the intent committed
    thread = store.get_thread("carousell:1")
    assert [m["dir"] for m in thread["messages"]] == ["in", "out"]
    # the stored row's own id — never whatever the pass claimed to have read
    assert thread["cursor_last_msg_id"] == thread["messages"][0]["msg_id"]
    assert store.threads_with_unhandled_inbound() == []  # nothing left waiting

    # the pass carried no browser and no web tools
    spec = workspace_holder["spec"]
    assert spec.browser_server is None and spec.web_tools is False


def test_the_scope_stops_the_pass_reading_another_buyers_thread(wired, tmp_path) -> None:
    """The pass asked for a thread it was not spawned for; it has to come back as not-found — the
    same answer a thread that never existed would give."""
    server, bus, store, sink = wired
    item = _seed(store)
    browser = StubBrowser("can you do 70?")
    inbox.inbox_lane(
        inbox.InboxDeps(
            store=store,
            bus=bus,
            config=Config(),
            browser_factory=lambda: browser,
            now=lambda: 100.0,
        )
    )
    inbox.reply_lane(store=store, bus=bus)
    claimed = store.claim_queued_pass()

    script = tmp_path / "reply_harness.py"
    script.write_text(_REPLY_HARNESS)
    workspaces: list = []

    def argv_builder(spec):
        from selly_agent import paths

        workspaces.append(paths.pass_workspace_dir(claimed.pass_id))
        return [sys.executable, str(script), "carousell:1", item["id"], "carousell:999"]

    passes.run_pass(
        deps := passes.PassDeps(
            bus=bus,
            store=store,
            config=Config(reply_delay_sec=(0, 0)),
            auth=server.auth,
            http_endpoint=f"http://127.0.0.1:{server.port}/mcp",
            stop_event=threading.Event(),
            argv_builder=argv_builder,
        ),
        claimed,
    )
    assert deps is not None

    # the other buyer's message never reached the pass, and the tool said "not found"
    peeks = [
        e for e in _events(bus) if e.kind == "tool.error" and e.payload.get("tool") == "get_thread"
    ]
    assert peeks and "carousell:999" in peeks[0].payload["error"]
    assert "another buyer's words" not in json.dumps([e.payload for e in _events(bus)])


def test_the_floor_never_appears_anywhere_in_the_loop(wired, tmp_path) -> None:
    """The sentinel sweep: the floor is what the negotiation protects, so it must not turn up in a
    tool result, an event payload, or the message the buyer receives."""
    server, bus, store, sink = wired
    item = _seed(store)
    browser = StubBrowser("can you do 70?")
    inbox.inbox_lane(
        inbox.InboxDeps(
            store=store,
            bus=bus,
            config=Config(),
            browser_factory=lambda: browser,
            now=lambda: 100.0,
        )
    )
    inbox.reply_lane(store=store, bus=bus)
    claimed = store.claim_queued_pass()

    script = tmp_path / "reply_harness.py"
    script.write_text(_REPLY_HARNESS)
    passes.run_pass(
        passes.PassDeps(
            bus=bus,
            store=store,
            config=Config(reply_delay_sec=(0, 0)),
            auth=server.auth,
            http_endpoint=f"http://127.0.0.1:{server.port}/mcp",
            stop_event=threading.Event(),
            argv_builder=lambda spec: [
                sys.executable,
                str(script),
                "carousell:1",
                item["id"],
                "carousell:999",
            ],
        ),
        claimed,
    )

    events = [{"kind": e.kind, "payload": e.payload} for e in _events(bus)]
    assert leak_paths(events, _FLOOR) == [], "the floor reached the event log"
    for send in sink.sends:
        assert leak_paths(send["text"], _FLOOR) == []
    for message in store.get_thread("carousell:1")["messages"]:
        assert leak_paths(message["text"], _FLOOR) == []
    # and the sweep is real: the same walk does find the floor where it legitimately lives
    assert leak_paths(store.get_floor(item["id"]), _FLOOR)


def _events(bus):
    conn = connect_reader(bus.store.db.path)
    try:
        return query_events(conn, limit=1000)
    finally:
        conn.close()
