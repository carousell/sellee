"""RailClient against a fake carousell.ai HTTP server: JSON-RPC create + fail-closed live verify."""

from __future__ import annotations

import json
import string
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.fake_mcp_eras import MODERN, EraLog, answer, decode

from sellee.rail.client import (
    RailAuthError,
    RailClient,
    RailNetworkError,
    RailToolError,
    RailToolRefused,
    _forget_eras,
    _text_content,
    listing_id_from_url,
)
from sellee.rail.mail_stream import StreamMissing


class _Handler(BaseHTTPRequestHandler):
    # behavior is set on the server instance (self.server) by each test
    def log_message(self, *args):  # silence the test server
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        if self.path.startswith("/upload"):
            self.server.uploads.append(
                {
                    "method": self.command,
                    "content_type": self.headers.get("Content-Type"),
                    "auth": self.headers.get("Authorization"),
                    "body": raw,
                }
            )
            self._send(self.server.upload_status, self.server.upload_result)
            return
        body = json.loads(raw) if raw else {}
        auth = self.headers.get("Authorization", "")
        if self.server.require_auth and auth != f"Bearer {self.server.expected_key}":
            self._send(401, {"error": "bad key"})
            return
        if answer(self, body, self.server.eras):
            return
        method = body.get("method")
        if method == "tools/call":
            self._send(200, {"jsonrpc": "2.0", "id": body["id"], "result": self.server.tool_result})
        else:
            self._send(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": {}})

    def do_GET(self):
        # the listing-page verify GET
        status = self.server.listing_status
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b"ok" if status == 200 else b"no")

    def _send(self, status, obj):
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def fake_rail():
    with _serving() as served:
        yield served


@contextmanager
def _serving(era: str = "legacy"):
    # A new server can reuse an earlier one's port, and so inherit its cached era.
    _forget_eras()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.eras = EraLog(era)
    server.require_auth = True
    server.expected_key = "guest-key"
    server.tool_result = {"structuredContent": {}}
    server.listing_status = 200
    server.uploads = []
    server.upload_status = 200
    server.upload_result = {"encrypted_url": "enc-abc"}
    # a short accept-loop poll: shutdown() waits for the next wake, and the stdlib default of
    # 0.5s would be paid by every test taking this fixture
    thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield server, base
    finally:
        server.shutdown()
        # shutdown() only stops the accept loop; the listening socket stays open without this.
        server.server_close()


def _client(base, key="guest-key"):
    return RailClient(api_base=base, api_key=key, web_base_url=base)


def test_create_listing(fake_rail) -> None:
    server, base = fake_rail
    url = f"{base}/listing/1-lamp"
    server.tool_result = {"structuredContent": {"listing_id": "L1", "url": url}}
    listing = _client(base).create_listing({"title": "Lamp", "price_cents": 8000})
    assert listing == {"listing_id": "L1", "url": url}


def test_create_listing_from_text_content(fake_rail) -> None:
    server, base = fake_rail
    server.tool_result = {"content": [{"type": "text", "text": json.dumps({"id": "L2"})}]}
    listing = _client(base).create_listing({"title": "x"})
    assert listing == {"listing_id": "L2", "url": f"{base}/listing/L2"}


def test_create_listing_reads_the_nested_listing_object(fake_rail) -> None:
    """The live shape: the created listing is nested, and carries no URL of its own."""
    server, base = fake_rail
    server.tool_result = {
        "structuredContent": {"listing": {"id": "L3", "status": "active", "images": []}}
    }
    listing = _client(base).create_listing({"title": "x"})
    assert listing == {"listing_id": "L3", "url": f"{base}/listing/L3"}


def test_create_listing_ignores_the_currency_the_response_carries(fake_rail) -> None:
    """The request asserts the recorded code, so nothing is read back off the listing."""
    server, base = fake_rail
    server.tool_result = {"structuredContent": {"listing": {"id": "L5", "currency": "VND"}}}
    assert "currency" not in _client(base).create_listing({"title": "x"})


def test_create_listing_prefers_a_url_the_rail_supplies(fake_rail) -> None:
    """Composing is the fallback — if the rail starts returning a URL, that one wins."""
    server, base = fake_rail
    given = f"{base}/listing/L4-slug"
    server.tool_result = {"structuredContent": {"listing": {"id": "L4", "url": given}}}
    assert _client(base).create_listing({"title": "x"})["url"] == given


def test_bad_key_raises_auth_error(fake_rail) -> None:
    _, base = fake_rail
    with pytest.raises(RailAuthError):
        _client(base, key="wrong").create_listing({"title": "x"})


def test_create_listing_without_an_id_is_tool_error(fake_rail) -> None:
    server, base = fake_rail
    server.tool_result = {"structuredContent": {"listing": {"status": "active"}}}  # no id
    with pytest.raises(RailToolError, match="no listing id"):
        _client(base).create_listing({"title": "x"})


def test_rail_reports_iserror_result(fake_rail) -> None:
    server, base = fake_rail
    server.tool_result = {"isError": True, "content": [{"type": "text", "text": "over quota"}]}
    with pytest.raises(RailToolError, match="over quota"):
        _client(base).create_listing({"title": "x"})


def test_verify_listing_url_live_200(fake_rail) -> None:
    server, base = fake_rail
    server.listing_status = 200
    _client(base).verify_listing_url(f"{base}/listing/1")  # no raise


def test_verify_fails_closed_on_non_200(fake_rail) -> None:
    server, base = fake_rail
    server.listing_status = 404
    with pytest.raises(RailToolError, match="404"):
        _client(base).verify_listing_url(f"{base}/listing/1")


def test_verify_rejects_wrong_base_or_path(fake_rail) -> None:
    _, base = fake_rail
    with pytest.raises(RailToolError, match="not under"):
        _client(base).verify_listing_url("https://evil.example/listing/1")
    with pytest.raises(RailToolError, match="not under"):
        _client(base).verify_listing_url(f"{base}/u/chat")


def test_text_content_helper() -> None:
    result = {
        "content": [
            {"type": "text", "text": "a"},
            {"type": "image"},
            {"type": "text", "text": "b"},
        ]
    }
    assert _text_content(result) == "ab"


# --- photo upload: mint, POST the bytes, read the reference off the upload response --------------


def _mint(server, base):
    server.tool_result = {"structuredContent": {"upload_url": f"{base}/upload?ticket=t1"}}


def test_upload_photo_posts_the_bytes_and_returns_the_upload_s_reference(fake_rail) -> None:
    """The reference is minted by the upload, not alongside the URL — the mint yields only a
    destination, so reading it from there would always come back empty."""
    server, base = fake_rail
    _mint(server, base)
    assert _client(base).upload_photo(b"\xff\xd8\xffbytes", "image/jpeg") == "enc-abc"

    (sent,) = server.uploads
    assert sent["method"] == "POST"  # the media endpoint refuses a PUT
    assert sent["content_type"] == "image/jpeg"
    assert sent["body"] == b"\xff\xd8\xffbytes"
    assert sent["auth"] is None  # pre-signed: our key never reaches the media host


def test_upload_photo_without_a_mint_url_is_a_tool_error(fake_rail) -> None:
    server, base = fake_rail
    server.tool_result = {"structuredContent": {}}
    with pytest.raises(RailToolError, match="no upload URL"):
        _client(base).upload_photo(b"x", "image/jpeg")


def test_upload_photo_without_a_reference_in_the_response_is_a_tool_error(fake_rail) -> None:
    server, base = fake_rail
    _mint(server, base)
    server.upload_result = {"ok": True}
    with pytest.raises(RailToolError, match="no media reference"):
        _client(base).upload_photo(b"x", "image/jpeg")


def test_upload_photo_surfaces_a_rejected_upload(fake_rail) -> None:
    server, base = fake_rail
    _mint(server, base)
    server.upload_status = 400
    with pytest.raises(RailToolError, match="400"):
        _client(base).upload_photo(b"x", "image/jpeg")


# --- update_listing: what is passed is exactly what travels --------------------------------------


class _ArgRecorder(_Handler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        if answer(self, body, self.server.eras):
            return
        if body.get("method") == "tools/call":
            self.server.calls.append(body["params"])
        self._send(200, {"jsonrpc": "2.0", "id": body["id"], "result": self.server.tool_result})


@pytest.fixture
def recording_rail(fake_rail):
    server, base = fake_rail
    server.calls = []
    server.RequestHandlerClass = _ArgRecorder
    return server, base


def test_update_listing_with_status_only_does_not_mention_urls(recording_rail) -> None:
    server, base = recording_rail
    _client(base).update_listing("L1", status="archived")
    (call,) = server.calls
    assert call["name"] == "update_listing"
    assert call["arguments"] == {"id": "L1", "status": "archived"}


def test_update_listing_with_urls_only_does_not_mention_status(recording_rail) -> None:
    server, base = recording_rail
    urls = {"urls": [{"platform": "EXTERNAL_PLATFORM_CAROUSELL", "url": "https://c.sg/p/1"}]}
    _client(base).update_listing("L1", external_urls=urls)
    (call,) = server.calls
    assert call["arguments"] == {"id": "L1", "external_urls": urls}


def test_update_listing_carries_both_when_both_are_passed(recording_rail) -> None:
    server, base = recording_rail
    _client(base).update_listing("L1", status="active", external_urls={"urls": []})
    (call,) = server.calls
    assert call["arguments"] == {"id": "L1", "status": "active", "external_urls": {"urls": []}}


def test_update_listing_with_neither_raises_before_any_call(recording_rail) -> None:
    server, base = recording_rail
    with pytest.raises(ValueError):
        _client(base).update_listing("L1")
    assert server.calls == []


def test_update_listing_carries_only_the_content_fields_passed(recording_rail) -> None:
    server, base = recording_rail
    _client(base).update_listing("L1", price_cents=12000, description="with the box")
    (call,) = server.calls
    # PATCH: title, media, status and the cross-links are absent, so the rail leaves them alone
    assert call["arguments"] == {"id": "L1", "price_cents": 12000, "description": "with the box"}


def test_update_listing_sends_a_replacement_photo_set(recording_rail) -> None:
    server, base = recording_rail
    media = {"urls": [{"url": "https://cdn/a.jpg", "type": 1}]}
    _client(base).update_listing("L1", title="Dyson V8", media=media)
    (call,) = server.calls
    assert call["arguments"] == {"id": "L1", "title": "Dyson V8", "media": media}


def test_update_listing_has_no_currency_argument(recording_rail) -> None:
    server, base = recording_rail
    with pytest.raises(TypeError):
        _client(base).update_listing("L1", currency="USD")  # type: ignore[call-arg]
    assert server.calls == []


def test_update_listing_sends_an_empty_set_as_present(recording_rail) -> None:
    """{"urls": []} travels — present-but-empty replaces the rail's whole set with nothing, which
    absent (unchanged) could never do."""
    server, base = recording_rail
    _client(base).update_listing("L1", external_urls={"urls": []})
    (call,) = server.calls
    assert call["arguments"]["external_urls"] == {"urls": []}


# --- create_promotion_url: the seller's sign-in link ---------------------------------------------


def test_create_promotion_url_returns_the_minted_link(recording_rail) -> None:
    server, base = recording_rail
    url = f"{base}/signin?flow=guest-promotion&promote=tok"
    server.tool_result = {"structuredContent": {"promotion_url": url}}
    assert _client(base).create_promotion_url() == {"promotion_url": url}
    (call,) = server.calls
    assert call["arguments"] == {}  # the account is the one the key belongs to


def test_create_promotion_url_without_a_url_is_a_tool_error(fake_rail) -> None:
    """No link means no link — never fabricate one for a credential-bearing URL."""
    server, base = fake_rail
    server.tool_result = {"structuredContent": {"ok": True}}
    with pytest.raises(RailToolError, match="no promotion URL"):
        _client(base).create_promotion_url()


def test_create_promotion_url_propagates_already_a_seller(fake_rail) -> None:
    """The transport does not interpret it — the tool layer decides that it means success."""
    server, base = fake_rail
    server.tool_result = {
        "isError": True,
        "content": [{"type": "text", "text": "already a seller"}],
    }
    with pytest.raises(RailToolError, match="already a seller"):
        _client(base).create_promotion_url()


# --- listing_id_from_url: the inverse of listing_url ----------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.carousell.ai/listing/abc123", "abc123"),
        ("https://www.carousell.ai/listing/abc123/teak-lamp", "abc123"),
        ("https://www.carousell.ai/listing/abc123?ref=share", "abc123"),
        ("https://www.carousell.ai/u/chat", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_listing_id_from_url(url, expected) -> None:
    assert listing_id_from_url(url) == expected


def test_listing_id_from_url_inverts_listing_url() -> None:
    client = RailClient(api_base="https://api.x", api_key="k", web_base_url="https://www.x")
    assert listing_id_from_url(client.listing_url("L9")) == "L9"


def test_every_rpc_accepts_both_json_and_event_stream(fake_rail) -> None:
    """The header the live rail rejects a request over — and it rejects *after* auth, so a wrong
    key hides it behind a 401."""
    server, base = fake_rail
    server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
    seen = {}

    class _Recorder(_Handler):
        def do_POST(self):
            seen["accept"] = self.headers.get("Accept")
            _Handler.do_POST(self)

    server.RequestHandlerClass = _Recorder
    _client(base).create_listing({"title": "x"})
    assert "application/json" in seen["accept"]
    assert "text/event-stream" in seen["accept"]


def test_offers_tool_reads_the_servers_tool_list():
    from tests.fake_carousell_ai_mcp import FakeRelay, serve

    relay_state = FakeRelay()
    base, shutdown = serve(relay_state)
    try:
        client = RailClient(api_base=base, api_key="k", web_base_url="https://www.carousell.ai")
        assert client.offers_tool("list_threads")
        assert not client.offers_tool("create_photo_upload_url")
    finally:
        shutdown()


# --- protocol eras: probe once, then speak the era the server speaks -----------------------------


def _headers(request) -> dict:
    return request["headers"]


@pytest.mark.parametrize("era", ["legacy", "modern"])
def test_create_listing_returns_the_same_listing_in_either_era(era) -> None:
    with _serving(era) as (server, base):
        server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
        assert _client(base).create_listing({"title": "x"}) == {
            "listing_id": "L1",
            "url": f"{base}/listing/L1",
        }


def test_a_modern_server_is_probed_once_and_spoken_to_in_the_new_era() -> None:
    with _serving("modern") as (server, base):
        server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
        _client(base).create_listing({"title": "x"})
        log = server.eras
        assert [r["method"] for r in log.requests] == ["server/discover", "tools/call"]
        (call,) = log.calls()
        assert _headers(call)["mcp-protocol-version"] == MODERN
        assert _headers(call)["mcp-method"] == "tools/call"
        assert _headers(call)["mcp-name"] == "create_listing"
        meta = call["params"]["_meta"]
        assert meta["io.modelcontextprotocol/protocolVersion"] == MODERN
        assert meta["io.modelcontextprotocol/clientCapabilities"] == {}
        assert meta["io.modelcontextprotocol/clientInfo"]["name"] == "sellee"


def test_a_legacy_server_is_initialized_once_and_spoken_to_in_2025_06_18() -> None:
    with _serving("legacy") as (server, base):
        server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
        client = _client(base)
        client.create_listing({"title": "x"})
        client.create_listing({"title": "y"})
        log = server.eras
        assert [r["method"] for r in log.requests] == [
            "server/discover",
            "initialize",
            "notifications/initialized",
            "tools/call",
            "tools/call",
        ]
        assert log.requests[1]["params"]["protocolVersion"] == "2025-06-18"
        for call in log.calls():
            assert _headers(call)["mcp-protocol-version"] == "2025-06-18"
            assert "_meta" not in call["params"]


def test_a_rejected_key_raises_and_leaves_the_era_unknown() -> None:
    with _serving("legacy") as (server, base):
        server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
        with pytest.raises(RailAuthError):
            _client(base, key="wrong").create_listing({"title": "x"})
        _client(base).create_listing({"title": "x"})
        assert server.eras.count("server/discover") == 1


def test_a_dropped_legacy_session_fails_that_call_and_renews_for_the_next() -> None:
    """The 404 cannot prove the call did not run, so it is never resent."""
    with _serving("legacy") as (server, base):
        server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
        server.eras.sessions = ["s1", "s2"]
        client = _client(base)
        client.create_listing({"title": "x"})
        server.eras.dropped.add("s1")
        with pytest.raises(RailNetworkError) as raised:
            client.create_listing({"title": "y"})
        assert raised.value.status == 404
        client.create_listing({"title": "z"})
        calls = server.eras.calls()
        assert [_headers(c).get("mcp-session-id") for c in calls] == ["s1", "s1", "s2"]
        assert server.eras.count("initialize") == 2


def test_concurrent_calls_on_a_dropped_session_renew_it_once() -> None:
    with _serving("legacy") as (server, base):
        server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
        server.eras.sessions = ["s1", "s2", "s3", "s4"]
        _client(base).create_listing({"title": "x"})
        server.eras.dropped.add("s1")

        def call():
            try:
                _client(base).create_listing({"title": "y"})
            except RailNetworkError:
                pass

        workers = [threading.Thread(target=call) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        assert server.eras.count("initialize") == 2


def test_a_modern_error_on_a_successful_probe_means_modern() -> None:
    with _serving("modern") as (server, base):
        server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
        server.eras.discover_error = -32021
        _client(base).create_listing({"title": "x"})
        assert server.eras.count("initialize") == 0
        (call,) = server.eras.calls()
        assert _headers(call)["mcp-protocol-version"] == MODERN


@settings(max_examples=25, deadline=None)
@given(
    era=st.sampled_from(["legacy", "modern"]),
    clients=st.integers(min_value=1, max_value=4),
    threads=st.integers(min_value=1, max_value=4),
)
def test_the_era_is_found_once_per_endpoint(era, clients, threads) -> None:
    """However many clients and threads share an endpoint, it is probed once."""
    with _serving(era) as (server, base):
        server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
        failures = []

        def work():
            try:
                for _ in range(clients):
                    _client(base).create_listing({"title": "x"})
            except Exception as exc:  # noqa: BLE001 - surfaced through the assertion below
                failures.append(exc)

        workers = [threading.Thread(target=work) for _ in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        assert failures == []
        assert server.eras.count("server/discover") == 1
        assert server.eras.count("initialize") == (1 if era == "legacy" else 0)
        assert len(server.eras.calls()) == clients * threads


_json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=20),
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=8), inner, max_size=3)
    ),
    max_leaves=10,
)


@settings(max_examples=40, deadline=None)
@given(
    era=st.sampled_from(["legacy", "modern"]),
    structured=st.dictionaries(st.text(max_size=8), _json_values, max_size=4),
)
def test_call_tool_returns_the_structured_content_in_either_era(era, structured) -> None:
    with _serving(era) as (server, base):
        server.tool_result = {"structuredContent": structured}
        assert _client(base).call_tool("get_thread", {"id": "t1"}) == structured


# Any name a server could list, including non-ASCII, padded and sentinel-shaped ones.
_tool_names = st.one_of(
    st.text(alphabet=string.ascii_letters + string.digits + "_-", min_size=1, max_size=40),
    st.text(min_size=1, max_size=20).filter(lambda n: "\x00" not in n),
    st.builds(lambda inner: f"=?base64?{inner}?=", st.text(string.ascii_letters, max_size=8)),
    st.builds(lambda inner: f" {inner} ", st.text(string.ascii_letters, max_size=8)),
)


@settings(max_examples=40, deadline=None)
@given(name=_tool_names, arguments=st.dictionaries(st.text(max_size=8), _json_values, max_size=4))
def test_modern_headers_match_the_body(name, arguments) -> None:
    with _serving("modern") as (server, base):
        server.tool_result = {"structuredContent": {}}
        _client(base).call_tool(name, arguments)
        for request in server.eras.requests:
            headers = _headers(request)
            assert headers["mcp-protocol-version"] == MODERN
            assert headers["mcp-method"] == request["method"]
            meta = request["params"]["_meta"]
            assert meta["io.modelcontextprotocol/protocolVersion"] == MODERN
        (call,) = server.eras.calls()
        assert decode(_headers(call)["mcp-name"]) == call["params"]["name"] == name
        assert call["params"]["arguments"] == arguments


def test_each_key_on_an_endpoint_finds_its_own_era() -> None:
    """A legacy session belongs to the key that opened it, so a rotated key never reuses it."""
    with _serving("legacy") as (server, base):
        server.require_auth = False
        server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
        server.eras.sessions = ["s1", "s2"]
        _client(base, key="old").create_listing({"title": "x"})
        _client(base, key="new").create_listing({"title": "y"})
        assert server.eras.count("initialize") == 2
        assert [_headers(c).get("mcp-session-id") for c in server.eras.calls()] == ["s1", "s2"]


def test_a_strict_legacy_server_gets_the_ready_notification_before_any_call() -> None:
    """A server that refuses calls until `notifications/initialized` serves every call, including
    on a session renewed after the server dropped the first one."""
    with _serving("legacy") as (server, base):
        server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
        server.eras.sessions = ["s1", "s2"]
        server.eras.require_ready = True
        client = _client(base)
        client.create_listing({"title": "x"})
        server.eras.dropped.add("s1")
        with pytest.raises(RailNetworkError):
            client.create_listing({"title": "y"})
        client.create_listing({"title": "z"})
        assert server.eras.ready == {"s1", "s2"}
        (ready,) = [r for r in server.eras.requests if r["method"] == "notifications/initialized"][
            :1
        ]
        assert _headers(ready)["mcp-protocol-version"] == "2025-06-18"
        assert _headers(ready)["mcp-session-id"] == "s1"


def test_a_method_not_found_at_200_means_legacy() -> None:
    """A legacy server that accepts the version header answers server/discover as an unknown
    method at HTTP 200, so the client initializes."""
    with _serving("legacy") as (server, base):
        server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
        server.eras.lenient = True
        _client(base).create_listing({"title": "x"})
        assert [r["method"] for r in server.eras.requests] == [
            "server/discover",
            "initialize",
            "notifications/initialized",
            "tools/call",
        ]


def test_a_method_not_found_at_404_means_modern() -> None:
    """A modern server answers a method it lacks with 404 and -32601, which the spec counts as a
    modern error, so the client stays modern."""
    with _serving("modern") as (server, base):
        server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
        server.eras.no_discover = True
        _client(base).create_listing({"title": "x"})
        assert server.eras.count("initialize") == 0
        (call,) = server.eras.calls()
        assert _headers(call)["mcp-protocol-version"] == MODERN


# --- the modern tool list is cached for the server's ttlMs ---------------------------------------


@contextmanager
def _relay(era: str, ttl_ms):
    from tests.fake_carousell_ai_mcp import FakeRelay, serve

    _forget_eras()
    state = FakeRelay()
    state.eras.era = era
    state.tools_ttl_ms = ttl_ms
    base, shutdown = serve(state)
    try:
        yield state, base
    finally:
        shutdown()


@contextmanager
def _clock(module):
    """A clock the test moves by hand, in seconds. Not `monkeypatch`: Hypothesis rejects a
    function-scoped fixture under @given."""
    now = [1000.0]
    original = module._monotonic
    module._monotonic = lambda: now[0]
    try:
        yield now
    finally:
        module._monotonic = original


@settings(max_examples=30, deadline=None)
@given(
    ttl_ms=st.integers(min_value=1, max_value=60_000),
    calls=st.integers(min_value=1, max_value=5),
    clients=st.integers(min_value=1, max_value=3),
)
def test_the_tool_list_is_fetched_once_per_ttl(ttl_ms, calls, clients) -> None:
    from sellee.rail import client as client_module

    with _relay("modern", ttl_ms) as (state, base), _clock(client_module) as now:
        for _ in range(clients):
            for _ in range(calls):
                assert _client(base).offers_tool("list_threads")
        assert state.eras.count("tools/list") == 1
        now[0] += ttl_ms / 1000
        assert _client(base).offers_tool("list_threads")
        assert state.eras.count("tools/list") == 2


@pytest.mark.parametrize(("era", "ttl_ms"), [("modern", 0), ("modern", None), ("legacy", 60_000)])
def test_the_tool_list_is_fetched_every_time_without_a_usable_ttl(era, ttl_ms) -> None:
    with _relay(era, ttl_ms) as (state, base):
        for _ in range(3):
            assert _client(base).offers_tool("list_threads")
            assert not _client(base).offers_tool("create_photo_upload_url")
        assert state.eras.count("tools/list") == 6


def test_concurrent_misses_fetch_the_tool_list_once() -> None:
    with _relay("modern", 60_000) as (state, base):
        workers = [
            threading.Thread(target=lambda: _client(base).offers_tool("list_threads"))
            for _ in range(6)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        assert state.eras.count("tools/list") == 1


# --- a server that changes era under a running client --------------------------------------------


def _switch(log, era: str) -> None:
    log.era = "modern" if era in ("modern", "modern_only") else "legacy"
    log.modern_only = era == "modern_only"


def test_a_server_that_drops_the_new_era_is_reprobed_and_the_call_runs_once() -> None:
    with _serving("modern") as (server, base):
        server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
        client = _client(base)
        client.create_listing({"title": "x"})
        _switch(server.eras, "legacy")
        client.create_listing({"title": "y"})
        assert server.eras.executed == 2
        assert server.eras.count("server/discover") == 2
        assert server.eras.count("initialize") == 1


@pytest.mark.parametrize("json_rejections", [False, True])
def test_a_server_that_drops_the_legacy_era_is_reprobed_and_the_call_runs_once(
    json_rejections,
) -> None:
    """go-sdk rejects a dropped version in plain text; the spec asks for JSON -32022. Either way the
    probe's listed versions show the legacy one is gone."""
    with _serving("legacy") as (server, base):
        server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
        server.eras.json_rejections = json_rejections
        client = _client(base)
        client.create_listing({"title": "x"})
        _switch(server.eras, "modern_only")
        client.create_listing({"title": "y"})
        assert server.eras.executed == 2
        assert server.eras.count("server/discover") == 2
        assert _headers(server.eras.calls()[-1])["mcp-protocol-version"] == MODERN


def test_a_failure_that_is_not_an_era_change_is_raised_and_never_resent() -> None:
    with _relay("modern", None) as (state, base):
        client = _client(base)
        client.list_threads("", 10)
        state.down = True
        with pytest.raises(RailNetworkError):
            client.list_threads("", 10)
        state.down = False
        client.list_threads("", 10)
        assert state.eras.count("server/discover") == 1


def test_a_modern_error_on_a_modern_call_is_not_an_era_change() -> None:
    """A header mismatch is the server speaking the new era; it is never cause to re-probe."""
    with _serving("modern") as (server, base):
        server.tool_result = {"structuredContent": {}}
        _client(base).call_tool("get_thread", {"id": "t1"})
        server.eras.refuse_calls = -32020
        with pytest.raises(RailToolRefused, match="-32020"):
            _client(base).call_tool("get_thread", {"id": "t1"})
        assert server.eras.count("tools/list") == 1
        assert server.eras.count("server/discover") == 1
        assert server.eras.executed == 1


@settings(max_examples=40, deadline=None)
@given(steps=st.lists(st.sampled_from(["legacy", "modern", "modern_only"]), min_size=1, max_size=6))
def test_every_call_runs_once_whatever_the_era_switches(steps) -> None:
    """Each era switch the server makes is either compatible or refused before any tool runs, so
    every call runs exactly once and each refusal costs exactly one re-probe."""
    with _serving(steps[0] if steps[0] != "modern_only" else "modern") as (server, base):
        server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
        client_modern = None
        contradictions = 0
        for step in steps:
            _switch(server.eras, step)
            if client_modern is not None:
                if (client_modern and step == "legacy") or (
                    not client_modern and step == "modern_only"
                ):
                    contradictions += 1
                    client_modern = step != "legacy"
            else:
                client_modern = step != "legacy"
            _client(base).create_listing({"title": "x"})
        assert server.eras.executed == len(steps)
        assert server.eras.count("server/discover") == 1 + contradictions


def test_a_400_from_a_server_still_in_the_era_is_raised_and_never_resent() -> None:
    """A modern server that refuses a call in plain text is probed, found modern still, and the
    call is not sent again: the 400 cannot prove the call did not run."""
    with _serving("modern") as (server, base):
        server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
        client = _client(base)
        client.create_listing({"title": "x"})
        server.eras.refuse_calls = "plain"
        with pytest.raises(RailNetworkError) as raised:
            client.create_listing({"title": "y"})
        assert raised.value.status == 400
        assert len(server.eras.calls()) == 2
        assert server.eras.count("server/discover") == 2


def test_a_legacy_400_from_a_dual_era_server_is_raised_and_never_resent() -> None:
    """The server still lists the legacy version, so the 400 was not about the era."""
    with _serving("legacy") as (server, base):
        server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
        client = _client(base)
        client.create_listing({"title": "x"})
        _switch(server.eras, "modern")
        server.eras.refuse_calls = "plain"
        with pytest.raises(RailNetworkError):
            client.create_listing({"title": "y"})
        assert len(server.eras.calls()) == 2


# --- Mcp-Param-* headers mirror a tool's x-mcp-header arguments ---------------------------------


def _annotated_tool(header="Region", kind="string", nested=False) -> dict:
    prop = {"type": kind, "x-mcp-header": header}
    properties = (
        {"where": {"type": "object", "properties": {"region": prop}}}
        if nested
        else {"region": prop}
    )
    return {
        "name": "execute",
        "inputSchema": {
            "type": "object",
            "properties": {**properties, "query": {"type": "string"}},
        },
    }


def _region(arguments: dict, nested: bool):
    return (arguments.get("where") or {}).get("region") if nested else arguments.get("region")


_header_values = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53) + 1, max_value=2**53 - 1),
    st.text(max_size=20).filter(lambda t: "\x00" not in t),
)


@settings(max_examples=50, deadline=None)
@given(value=_header_values, nested=st.booleans())
def test_mcp_param_headers_carry_every_annotated_argument(value, nested) -> None:
    """The modern fake refuses a call whose Mcp-Param-* headers do not decode to the body, so a
    served call is the proof; the header is also read back here."""
    kind = (
        "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "string"
    )
    with _serving("modern") as (server, base):
        server.tool_result = {"structuredContent": {"ok": True}}
        server.eras.tools = [_annotated_tool(kind=kind, nested=nested)]
        client = _client(base)
        client.offers_tool("execute")
        args = {"where": {"region": value}} if nested else {"region": value}
        assert client.call_tool("execute", {**args, "query": "q"}) == {"ok": True}
        (call,) = server.eras.calls()
        header = _headers(call).get("mcp-param-region")
        if _region(call["params"]["arguments"], nested) is None:
            assert header is None
        else:
            text = ("true" if value else "false") if isinstance(value, bool) else str(value)
            assert decode(header) == text


def test_a_header_mismatch_refreshes_the_tool_list_and_retries_once() -> None:
    """The server added an annotation after the client cached its tool list."""
    with _serving("modern") as (server, base):
        server.tool_result = {"structuredContent": {"ok": True}}
        server.eras.tools = [{"name": "execute", "inputSchema": {"type": "object"}}]
        client = _client(base)
        client.offers_tool("execute")
        server.eras.tools = [_annotated_tool()]
        assert client.call_tool("execute", {"region": "us-west1"}) == {"ok": True}
        assert server.eras.count("tools/list") == 2
        assert len(server.eras.calls()) == 2
        assert server.eras.executed == 1
        assert _headers(server.eras.calls()[-1])["mcp-param-region"] == "us-west1"


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"r": {"type": "number", "x-mcp-header": "R"}}},
        {"type": "object", "properties": {"r": {"type": "string", "x-mcp-header": ""}}},
        {"type": "object", "properties": {"r": {"type": "string", "x-mcp-header": "Bad Name"}}},
        {
            "type": "object",
            "properties": {
                "a": {"type": "string", "x-mcp-header": "Same"},
                "b": {"type": "string", "x-mcp-header": "same"},
            },
        },
        {
            "type": "object",
            "properties": {
                "l": {"type": "array", "items": {"type": "string", "x-mcp-header": "Item"}}
            },
        },
    ],
)
def test_a_tool_with_an_invalid_header_annotation_is_left_out(schema) -> None:
    with _serving("modern") as (server, base):
        server.eras.tools = [
            {"name": "broken", "inputSchema": schema},
            {"name": "fine", "inputSchema": {"type": "object"}},
        ]
        client = _client(base)
        assert client.offers_tool("fine")
        assert not client.offers_tool("broken")


@pytest.mark.parametrize(
    ("value", "sent"),
    [
        ("us-west1", "us-west1"),
        ("Hello, 世界", "=?base64?SGVsbG8sIOS4lueVjA==?="),
        (" padded ", "=?base64?IHBhZGRlZCA=?="),
        ("line1\nline2", "=?base64?bGluZTEKbGluZTI=?="),
        ("=?base64?literal?=", "=?base64?PT9iYXNlNjQ/bGl0ZXJhbD89?="),
        ("a\tb", "=?base64?YQli?="),
    ],
)
def test_param_values_are_encoded_as_the_spec_shows(value, sent) -> None:
    """The spec's own encoding table, plus an inner tab, which is a control character."""
    with _serving("modern") as (server, base):
        server.tool_result = {"structuredContent": {"ok": True}}
        server.eras.tools = [_annotated_tool()]
        client = _client(base)
        client.offers_tool("execute")
        client.call_tool("execute", {"region": value})
        assert _headers(server.eras.calls()[-1])["mcp-param-region"] == sent


@pytest.mark.parametrize(("value", "sent"), [(5.0, "5"), (True, "true"), (42, "42")])
def test_param_values_take_their_header_form(value, sent) -> None:
    with _serving("modern") as (server, base):
        server.tool_result = {"structuredContent": {"ok": True}}
        server.eras.tools = [_annotated_tool(kind="integer")]
        client = _client(base)
        client.offers_tool("execute")
        try:
            client.call_tool("execute", {"region": value})
        except RailToolRefused:
            pass
        assert _headers(server.eras.calls()[0])["mcp-param-region"] == sent


def test_a_non_ascii_tool_name_against_go_sdk_fails_after_one_refresh() -> None:
    """go-sdk v1.8.0 compares Mcp-Name undecoded, so the Base64 name the spec requires is refused;
    the client reads the tool list once, sends once more, then raises."""
    with _serving("modern") as (server, base):
        server.tool_result = {"structuredContent": {"ok": True}}
        server.eras.literal_names = True
        with pytest.raises(RailToolRefused, match="-32020"):
            _client(base).call_tool("café", {})
        assert len(server.eras.calls()) == 2
        assert server.eras.count("tools/list") == 1
        assert server.eras.executed == 0


@pytest.mark.parametrize(("start", "found"), [("legacy", "modern_only"), ("modern", "legacy")])
def test_a_400_that_is_not_a_version_refusal_is_never_resent(start, found) -> None:
    """A backend that ran the call and then failed with a plain 400, while the probe reaches a
    server in the other era, as during a rolling deploy."""

    class _RanThenFailed(_Handler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            eras = self.server.eras
            if body.get("method") == "tools/call" and not self.server.failed_once:
                self.server.failed_once = True
                self.server.ran += 1
                eras.era, eras.modern_only = (
                    ("modern", True) if found == "modern_only" else (found, False)
                )
                payload = b"Bad Request"
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if answer(self, body, eras):
                return
            if body.get("method") == "tools/call":
                self.server.ran += 1
            self._send(200, {"jsonrpc": "2.0", "id": body["id"], "result": self.server.tool_result})

    with _serving(start) as (server, base):
        server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
        client = _client(base)
        assert client.offers_tool("create_listing") or True
        server.RequestHandlerClass = _RanThenFailed
        server.failed_once, server.ran = False, 0
        with pytest.raises(RailNetworkError) as raised:
            client.call_tool("create_listing", {"title": "x"})
        assert raised.value.status == 400
        assert server.ran == 1
        # The probe still moved the cache, so the next call goes out in the server's era.
        assert client.call_tool("create_listing", {"title": "y"}) == {"listing": {"id": "L1"}}
        assert server.ran == 2


# 307 and 308 to a POST were refused before the fix too; they pin the property, not the change.
_REDIRECT_STATUSES = [301, 302, 303, 307, 308]


def _drain(handler) -> None:
    length = int(handler.headers.get("Content-Length", 0))
    if length:
        handler.rfile.read(length)


class _Target(BaseHTTPRequestHandler):
    """The host a redirect points at; it records anything that reaches it, and would answer."""

    def log_message(self, *args):
        pass

    def _record(self):
        _drain(self)
        self.server.seen.append((self.command, self.path, self.headers.get("Authorization")))
        payload = b'{"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"evil": true}}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _record


@contextmanager
def _target():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Target)
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _redirecting(status: int, location: str):
    class _Redirect(_Handler):
        def _away(self):
            _drain(self)
            self.send_response(status)
            self.send_header("Location", location + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = _away

    return _Redirect


@pytest.mark.parametrize("cached", [False, True], ids=["first-request", "after-era-cached"])
@pytest.mark.parametrize("era", ["legacy", "modern"])
@pytest.mark.parametrize("status", _REDIRECT_STATUSES)
def test_a_redirect_from_mcp_is_never_followed(status, era, cached) -> None:
    with _target() as (target, elsewhere), _serving(era) as (server, base):
        server.tool_result = {"structuredContent": {"listing": {"id": "L1"}}}
        client = _client(base)
        if cached:
            client.call_tool("create_listing", {"title": "x"})
        server.RequestHandlerClass = _redirecting(status, elsewhere)
        with pytest.raises(RailNetworkError) as raised:
            client.call_tool("create_listing", {"title": "y"})
        assert raised.value.status == status
        assert target.seen == []


@pytest.mark.parametrize("status", _REDIRECT_STATUSES)
def test_a_redirect_from_the_mail_stream_is_never_followed(fake_rail, status) -> None:
    server, base = fake_rail
    with _target() as (target, elsewhere):
        server.RequestHandlerClass = _redirecting(status, elsewhere)
        stream = _client(base).open_mail_stream()
        with pytest.raises(StreamMissing, match=str(status)):
            list(stream.events())
        assert target.seen == []
