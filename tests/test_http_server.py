"""The localhost HTTP server: Host/Origin guard, bearer auth, MCP JSON-RPC, control, web tail."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

import pytest

import sellee.http_server as http_server
import sellee.tools  # noqa: F401  registration
from sellee.config import Config
from sellee.http_server import (
    _MAX_BODY_BYTES,
    _PAGE_EVENTS,
    HttpServer,
    _Handler,
    _localhost_host,
    _Server,
)
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


def _raw_request(*, token=None, content_length=None, body=b""):
    """A raw POST /mcp for what urllib refuses to send: a Content-Length that lies."""
    declared = str(len(body)) if content_length is None else str(content_length)
    head = [
        "POST /mcp HTTP/1.1",
        "Host: 127.0.0.1",
        "Content-Type: application/json",
        f"Content-Length: {declared}",
    ]
    if token is not None:
        head.append(f"Authorization: Bearer {token}")
    return ("\r\n".join(head) + "\r\n\r\n").encode() + body


def _read_raw_response(reader):
    """(status, headers) for one response off a raw connection, or None once it closed."""
    status_line = reader.readline()
    if not status_line:
        return None
    status = int(status_line.split()[1])
    headers = {}
    while True:
        line = reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        name, _, value = line.decode().partition(":")
        headers[name.strip().lower()] = value.strip()
    reader.read(int(headers.get("content-length", "0")))
    return status, headers


def _raw_exchange(server, payload, *, timeout=5):
    """One raw request, one response — or a failure rather than a wait. The timeout is the
    assertion: a handler that reads a body it was lied to about never answers at all, and this
    has to fail the case instead of hanging the suite."""
    conn = socket.create_connection(("127.0.0.1", server.port), timeout=timeout)
    try:
        conn.sendall(payload)
        try:
            return _read_raw_response(conn.makefile("rb"))
        except TimeoutError as exc:
            raise AssertionError("no response: the request pinned the handler thread") from exc
    finally:
        conn.close()


# --- hardening --------------------------------------------------------------------------------


def test_bad_host_is_forbidden(server) -> None:
    status, _ = _request(
        server, "POST", "/mcp", token="attended-secret", body={}, headers={"Host": "evil.example"}
    )
    assert status == 403


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("localhost", True),
        ("localhost:7355", True),
        ("127.0.0.1", True),
        ("127.0.0.1:7355", True),
        ("[::1]", True),
        ("[::1]:7355", True),
        ("[::ffff:127.0.0.1]", False),
        ("evil.com:7355", False),
        ("[evil.com]:80", False),
        ("127.0.0.1.evil.com", False),
        ("evil@localhost", False),
        ("", False),
        (None, False),
    ],
)
def test_localhost_host_guard_accepts_only_localhost_names(host, allowed) -> None:
    assert _localhost_host(host) is allowed


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


def test_an_oversized_content_length_is_refused_before_any_body_is_read(server) -> None:
    """The declaration alone is enough to refuse — note the request below sends no body at all,
    so a 413 can only come from a check that ran ahead of the read."""
    status, headers = _raw_exchange(
        server, _raw_request(token="attended-secret", content_length=_MAX_BODY_BYTES + 1)
    )
    assert status == 413
    assert headers.get("connection") == "close"  # the undrained bytes leave nothing reusable


def test_a_negative_content_length_is_a_clean_400_and_does_not_hang(server) -> None:
    """int("-1") is truthy and read(-1) reads until the peer closes, so this declaration used to
    hold a handler thread for as long as the client cared to keep the socket open."""
    status, headers = _raw_exchange(
        server, _raw_request(token="attended-secret", content_length=-1)
    )
    assert status == 400
    assert headers.get("connection") == "close"


def test_a_non_numeric_content_length_is_a_clean_400(server) -> None:
    """It used to come back as a JSON-RPC parse error: the ValueError from int() fell into the
    except that was there for json.loads."""
    status, _ = _raw_exchange(
        server, _raw_request(token="attended-secret", content_length="banana")
    )
    assert status == 400


def test_a_body_that_never_arrives_does_not_hold_the_thread_forever(server, monkeypatch) -> None:
    """The cap bounds how *much* a connection may send, not how *long* it may take over it. A
    declared length that is never delivered pins a thread exactly like the negative one did, so
    the handler carries a deadline. Shortened here so the test does not wait out the real one."""
    assert _Handler.timeout is not None  # the shipped value is what bounds this in production
    monkeypatch.setattr(_Handler, "timeout", 0.3)
    conn = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    try:
        # a legal declaration, then silence: one byte of a body that claims to be a hundred
        conn.sendall(_raw_request(token="attended-secret", content_length=100, body=b"{"))
        # the read gives up and the connection goes away, rather than waiting for a body forever
        assert conn.recv(4096) == b""
    finally:
        conn.close()


def test_a_trickled_body_runs_out_of_deadline_not_patience(server, monkeypatch) -> None:
    """The idle timeout resets on every byte, so a client feeding one byte per interval could
    hold a thread for length × interval. The body carries one deadline for its whole read;
    trickling inside the idle window still ends at that deadline with a closed connection."""
    monkeypatch.setattr(http_server, "_BODY_DEADLINE_SEC", 0.4)
    conn = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    try:
        conn.sendall(_raw_request(token="attended-secret", content_length=1000, body=b"{"))
        for _ in range(10):  # ~1s of drip-feed, each send well inside the idle timeout
            try:
                conn.sendall(b"x")
            except OSError:
                break  # the server already hung up — the deadline worked
            time.sleep(0.1)
        conn.settimeout(2)
        assert conn.recv(4096) == b""  # closed by the deadline, not still waiting on the body
    finally:
        conn.close()


def test_a_bad_content_length_pre_auth_is_still_unauthorized(server) -> None:
    """The body checks sit behind the bearer, not in front of it: no token is still a 401,
    whatever the request claims about its size."""
    for declared in (_MAX_BODY_BYTES + 1, -1, "banana"):
        status, _ = _raw_exchange(server, _raw_request(content_length=declared))
        assert status == 401, declared


def test_a_401_drains_the_body_so_the_connection_stays_usable(server) -> None:
    """Bytes left unread on a keep-alive connection get parsed as the next request line, so a
    client whose token expired mid-session got a broken connection instead of a clean 401."""
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    conn = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    try:
        conn.sendall(_raw_request(body=b"HELLO"))
        conn.sendall(_raw_request(token="attended-secret", body=ping))
        reader = conn.makefile("rb")
        assert _read_raw_response(reader)[0] == 401
        assert _read_raw_response(reader)[0] == 200  # not 501 on a "HELLOPOST" request line
    finally:
        conn.close()


# --- MCP protocol -----------------------------------------------------------------------------


def test_initialize_advertises_tools_only(server) -> None:
    status, body = _rpc(server, "initialize", {"protocolVersion": "2025-06-18"})
    assert status == 200
    assert body["result"]["capabilities"] == {"tools": {}}
    assert body["result"]["serverInfo"]["name"] == "sellee"


def test_initialize_echoes_a_supported_protocol_version(server) -> None:
    status, body = _rpc(server, "initialize", {"protocolVersion": "2025-06-18"})
    assert status == 200
    assert body["result"]["protocolVersion"] == "2025-06-18"


@pytest.mark.parametrize(
    "params",
    [
        {"protocolVersion": "9999.invalid"},
        # a real older revision, but one whose transport this server does not implement: the
        # honest answer is the version we do speak, not the one that would please the client
        {"protocolVersion": "2024-11-05"},
        {"protocolVersion": {"a": 1}},
        {"protocolVersion": ["x"]},
        {"protocolVersion": 12345},
        {"protocolVersion": None},
        {},
    ],
)
def test_initialize_falls_back_to_the_default_protocol_version(server, params) -> None:
    status, body = _rpc(server, "initialize", params)
    assert status == 200
    negotiated = body["result"]["protocolVersion"]
    assert isinstance(negotiated, str)
    assert negotiated == "2025-06-18"


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
    for market in ("carousel", "mercari", "ebay"):
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
        # The buyer page's own fetches, on the same footing as the tail page's: the page is
        # navigated to as /buyer?ticket=… and trades that one-shot ticket for the token before
        # either of these runs, so no token ever reaches the address bar. sim-items is a CLI read
        # too — `sellee buyer` calls it to check the simulator is on before opening anything.
        "_handle_sim_items",
        "_handle_sim_thread",
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


def _raw_post(server, path, *, headers, timeout=2.0):
    """A POST whose Content-Length is ours to choose — urllib always computes an honest one.

    Returns the first response bytes, or None if the daemon never answered (which is the failure
    this exists to catch: a handler blocked in rfile.read, holding its thread).
    """
    import socket

    sock = socket.create_connection(("127.0.0.1", server.port), timeout=timeout)
    try:
        sock.sendall(f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{headers}\r\n".encode())
        try:
            return sock.recv(200)
        except (TimeoutError, socket.timeout):
            return None
    finally:
        sock.close()


def test_a_negative_content_length_cannot_hang_a_thread_without_a_token(server) -> None:
    """The ticket exchange reads a body before it has any credential to check, so `read(-1)` —
    which blocks until the peer closes — was reachable with nothing at all. One wedged thread
    per connection, on an uncapped pool."""
    server.auth.mint_tail_ticket()
    answer = _raw_post(server, "/control/tail-exchange", headers="Content-Length: -1\r\n")
    assert answer is not None, "the handler never answered — it is blocked reading the body"
    assert b"400" in answer


def test_a_garbled_content_length_is_refused_rather_than_raising(server) -> None:
    answer = _raw_post(server, "/control/tail-exchange", headers="Content-Length: 12x\r\n")
    assert answer is not None
    assert b"400" in answer


def test_an_oversized_body_is_refused_before_it_is_read(server) -> None:
    from sellee.http_server import _MAX_BODY_BYTES

    answer = _raw_post(
        server, "/control/tail-exchange", headers=f"Content-Length: {_MAX_BODY_BYTES + 1}\r\n"
    )
    assert answer is not None
    assert b"413" in answer


def test_the_authenticated_routes_get_the_same_guard(server) -> None:
    """The guard belongs to the body read, not to one route: /mcp and the attended control
    routes announce a length too, and neither should be believed on faith either."""
    for path in ("/mcp", "/control/tail-ticket"):
        answer = _raw_post(
            server,
            path,
            headers="Authorization: Bearer attended-secret\r\nContent-Length: -1\r\n",
        )
        assert answer is not None, path
        assert b"400" in answer, path


def test_a_body_within_the_cap_still_works(server) -> None:
    """The cap must not clip real traffic: a genuine attended POST is unaffected."""
    status, body = _request(
        server, "POST", "/control/tail-ticket", token="attended-secret", body={}
    )
    assert status == 200 and body["ticket"]


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
