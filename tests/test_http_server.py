"""The localhost HTTP server: Host/Origin guard, bearer auth, MCP JSON-RPC, control, web tail."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

import sellee.tools  # noqa: F401  registration
from sellee.config import Config
from sellee.http_server import _PAGE_EVENTS, HttpServer, _Handler, _Server
from sellee.paths import PACKAGE_DATA_DIR
from sellee.tools.registry import ToolContext


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
    assert body["result"]["serverInfo"]["name"] == "sellee"


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
        # the selector-cache surface: a browser-driving pass heals its own selectors
        "ui_cache_get",
        "ui_cache_record",
        "ui_cache_invalidate",
        "probe_selector",
        "record_published_listing_url",
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


def test_enqueue_pass_refuses_a_market_it_cannot_publish_to(server, store, bus) -> None:
    """A mistyped market used to spawn a real pass told to publish in a browser it never got, with
    no recipe. The caller is still on the other end of this request, so they hear it here."""
    store.set_seller_config_section("basics", {"region": "SG"})
    for market in ("carousel", "fb", "ebay"):
        status, body = _request(
            server,
            "POST",
            "/control/enqueue-pass",
            token="attended-secret",
            body={"type": "publish", "payload": {"item_id": "item_1", "market": market}},
        )
        assert status == 400, market
        assert market in body["error"]
    assert [e for e in bus.store.read() if e.kind == "pass.queued"] == []


def test_enqueue_pass_accepts_the_rail_and_a_publishable_market(server, store) -> None:
    store.set_seller_config_section("basics", {"region": "SG"})
    for market in ("carousell-ai", "carousell"):
        status, _ = _request(
            server,
            "POST",
            "/control/enqueue-pass",
            token="attended-secret",
            body={"type": "publish", "payload": {"item_id": "item_1", "market": market}},
        )
        assert status == 200, market


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
    assert all("@ts" in e for e in body["events"])  # rows share the logs --json wire shape


def test_events_json_since_sec_windows_the_history(server, bus) -> None:
    old = bus.publish("demo.old", {})
    bus.publish("demo.recent", {})
    # back-date the first event past the window; ts is the store's own ordering column
    with bus.store.db.transaction() as conn:
        conn.execute("UPDATE events SET ts = ts - 7200 WHERE seq = ?", (old.seq,))
    status, body = _request(
        server, "GET", "/events.json?token=attended-secret&after_seq=0&since_sec=3600"
    )
    assert status == 200
    kinds = [e["kind"] for e in body["events"]]
    assert "demo.recent" in kinds and "demo.old" not in kinds
    # a garbage or non-positive window is simply no window at all
    for raw in ("0", "-5", "nonsense"):
        _, body = _request(
            server, "GET", f"/events.json?token=attended-secret&after_seq=0&since_sec={raw}"
        )
        assert "demo.old" in [e["kind"] for e in body["events"]]


def test_events_json_seeds_with_the_newest_page(server, bus) -> None:
    """No after_seq means a page is opening: it gets the newest events, not the oldest, so a tail
    shows now instead of paging forward through the history one poll at a time."""
    for i in range(_PAGE_EVENTS + 20):
        bus.publish("demo.event", {"i": i})
    status, body = _request(server, "GET", "/events.json?token=attended-secret")
    assert status == 200
    assert len(body["events"]) == _PAGE_EVENTS
    seen = [e["payload"]["i"] for e in body["events"]]
    assert seen[-1] == _PAGE_EVENTS + 19  # the newest event is present
    assert seen == sorted(seen)  # still answered oldest-first within the page
    # and the cursor is at the ceiling, so following starts clean rather than replaying
    assert body["last_seq"] == max(e["seq"] for e in body["events"])


def test_events_json_never_sends_the_routine_tier(server, bus) -> None:
    """The heartbeat is the bulk of the volume and none of what a tail is opened to read."""
    bus.publish("task.start", {"task": "pass_lane"})
    bus.publish("task.ok", {"task": "pass_lane"})
    bus.publish("demo.event", {})
    _, body = _request(server, "GET", "/events.json?token=attended-secret")
    kinds = [e["kind"] for e in body["events"]]
    assert "demo.event" in kinds
    assert "task.start" not in kinds and "task.ok" not in kinds
    assert all(e["level"] != "routine" for e in body["events"])


def test_events_json_cursor_clears_the_routine_it_skipped(server, bus) -> None:
    """The cursor tracks what was considered, not what was returned: parked below a run of skipped
    heartbeat rows it would rescan them on every poll for as long as the page stayed open."""
    bus.publish("demo.event", {})
    _, first = _request(server, "GET", "/events.json?token=attended-secret")
    for _ in range(50):
        bus.publish("task.ok", {"task": "pass_lane"})
    ceiling = bus.publish("task.ok", {"task": "pass_lane"}).seq

    _, body = _request(
        server, "GET", f"/events.json?token=attended-secret&after_seq={first['last_seq']}"
    )
    assert body["events"] == []  # nothing worth showing happened
    assert body["last_seq"] == ceiling  # but the cursor moved past all of it


def test_tail_serves_the_packaged_page(server) -> None:
    url = f"http://127.0.0.1:{server.port}/tail?token=attended-secret"
    with urllib.request.urlopen(url, timeout=5) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/html")
        served = resp.read()
    assert b"event tail" in served
    # the page is a packaged asset, not an inline string — pin that wiring
    assert served == (PACKAGE_DATA_DIR / "tail.html").read_bytes()


# --- the tail ticket exchange and token rotation ------------------------------------------------


def test_the_tail_ticket_mint_requires_the_attended_token(server) -> None:
    status, _ = _request(server, "POST", "/control/tail-ticket", body={})
    assert status == 401


def test_a_tail_ticket_redeems_exactly_once(server) -> None:
    status, body = _request(
        server, "POST", "/control/tail-ticket", token="attended-secret", body={}
    )
    assert status == 200
    ticket = body["ticket"]
    assert ticket != "attended-secret"

    status, body = _request(server, "POST", "/control/tail-exchange", body={"ticket": ticket})
    assert status == 200
    assert body["token"] == "attended-secret"

    # A second redemption — a URL lifted from history or scrollback — is dead.
    status, _ = _request(server, "POST", "/control/tail-exchange", body={"ticket": ticket})
    assert status == 401


def test_an_expired_or_unknown_tail_ticket_is_unauthorized(server) -> None:
    from sellee.http_server import _TAIL_TICKET_TTL_SEC

    status, _ = _request(server, "POST", "/control/tail-exchange", body={"ticket": "nope"})
    assert status == 401

    ticket = server.auth.mint_tail_ticket(now=1000.0)
    assert server.auth.redeem_tail_ticket(ticket, now=1000.0 + _TAIL_TICKET_TTL_SEC + 1) is None
    # and expiry consumed it — it does not come back to life
    assert server.auth.redeem_tail_ticket(ticket, now=1000.0) is None


def test_rotating_the_token_kills_the_old_one_live(server) -> None:
    status, _ = _request(server, "POST", "/control/rotate-token", token="attended-secret", body={})
    assert status == 200

    from sellee import secrets

    new_token = secrets.read_mcp_token()
    assert new_token and new_token != "attended-secret"
    # the old token is dead on the running server, the new one works — no restart
    assert _rpc(server, "ping", token="attended-secret")[0] == 401
    assert _rpc(server, "ping", token=new_token)[0] == 200


def test_query_string_tokens_stay_off_browser_navigable_surfaces() -> None:
    """A token in a URL lands in the address bar and history, so _attended_query is only for
    CLI reads and the tail page's own fetch (not a navigation). Anything a browser navigates
    to gets the ticket exchange instead. Growing this set is a deliberate act."""
    import inspect

    from sellee import http_server

    callers = {
        name
        for name, fn in inspect.getmembers(http_server._Handler, inspect.isfunction)
        if name != "_attended_query" and "_attended_query(" in inspect.getsource(fn)
    }
    assert callers == {
        "_handle_events_json",  # the tail page's own fetch
        "_handle_channel_status",  # CLI-only
        "_handle_settings_list",  # CLI-only
        "_handle_seller_basics_read",  # CLI-only
    }


def test_a_non_ascii_ticket_is_a_mismatch_not_a_crash(server) -> None:
    """compare_digest raises on a non-ASCII str instead of answering False, and the ticket comes
    straight out of a JSON body — so this used to kill the handler with no credential at all,
    once any ticket was outstanding to compare against."""
    server.auth.mint_tail_ticket()
    status, _ = _request(server, "POST", "/control/tail-exchange", body={"ticket": "café"})
    assert status == 401


def test_a_non_ascii_bearer_is_a_mismatch_not_a_crash(server) -> None:
    """The same edge on the header path: headers decode as latin-1, so a high byte arrives as a
    non-ASCII str here too."""
    status, _ = _request(server, "POST", "/control/tail-ticket", token="café", body={})
    assert status == 401


def test_the_market_routes_reject_get(server) -> None:
    """They navigate the shared tab: a side effect must not be reachable by a URL alone, and a
    top-level navigation sends no bearer and no Origin."""
    for path in ("/control/market-login", "/control/market-logins"):
        status, _ = _request(server, "GET", f"{path}?market=carousell&token=attended-secret")
        assert status == 405, path


def test_a_request_with_no_origin_cannot_reach_a_side_effecting_route(server) -> None:
    """Origin-absent requests pass the localhost guard (a CLI sends none) — the bearer is what
    stands between a navigation and the browser-driving routes."""
    status, _ = _request(server, "POST", "/control/market-login", body={"market": "carousell"})
    assert status == 401


# --- binding -------------------------------------------------------------------------------------


def test_the_bind_never_asks_the_network_who_we_are(monkeypatch) -> None:
    """Where the machine has no reverse record for its own address, the getfqdn() the stdlib's
    bind performs leaves the daemon having recorded daemon.start and then answering nothing — not
    its stop route, not SIGTERM. Both CI runners reproduce it."""
    import socket

    def explode(*args):
        raise AssertionError("server_bind performed a reverse-DNS lookup")

    monkeypatch.setattr(socket, "getfqdn", explode)

    httpd = _Server(("127.0.0.1", 0), _Handler)
    try:
        assert httpd.server_name == "127.0.0.1"
        assert httpd.server_port == httpd.server_address[1]
    finally:
        httpd.server_close()
