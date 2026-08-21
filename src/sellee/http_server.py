"""The daemon's one localhost HTTP server: MCP endpoint, web tail, and the pass-control route.

Bound to 127.0.0.1 unless SELLEE_BIND_HOST says otherwise — a container binds the wildcard so a
browser on the host reaches the tail through a published port. The Host guard below does not move
with it: however this binds, requests still have to arrive addressed to localhost. Three surfaces
share it:

  * POST /mcp   — stateless MCP over streamable HTTP (plain-JSON responses, no SSE). JSON-RPC 2.0
                  with initialize / notifications/initialized / tools/list / tools/call / ping.
  * GET  /events.json, GET /tail — the localhost web tail, reading the event store over a
                  read-only connection.
  * POST /control/enqueue-pass  — enqueue a pass (attended token only).

Hardening from the first line: every request's Host must be a localhost name and any Origin
header must be a localhost origin (DNS-rebinding defense); bearer tokens map to a session tier
via constant-time comparison; a 401 never echoes the presented token. Tier filtering happens on
both tools/list and tools/call — a tool a session can't see is indistinguishable from one that
does not exist.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets as _stdlib_secrets
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from sellee import __version__, passes
from sellee.db import connect_reader
from sellee.events import event_to_wire, latest_seq, query_events, routine_kinds
from sellee.paths import PACKAGE_DATA_DIR
from sellee.tools.registry import Session, ToolError, UnknownTool, dispatch, tools_for_tier

log = logging.getLogger(__name__)

# The web tail's page: a packaged asset, not an inline string — it is a real HTML/CSS/JS document.
_TAIL_PAGE = PACKAGE_DATA_DIR / "tail.html"

_LOCALHOST_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})

# Pass types that drive the browser. A login probe navigates the daemon's one tab, so it waits
# rather than pulling the page out from under one of these.
_BROWSER_PASS_TYPES = ("reply", "publish")
_DEFAULT_PROTOCOL_VERSION = "2025-06-18"

# How long a minted web-tail ticket stays redeemable. Long enough to copy the printed URL into a
# browser by hand; short enough that a URL lifted from a terminal scrollback later is dead.
_TAIL_TICKET_TTL_SEC = 300

# The largest request body any route here has a use for. Control bodies are a handful of fields
# and the largest MCP argument is a message the model composed, so this is orders of magnitude
# above real traffic and exists only so an announced length cannot be believed on faith.
_MAX_BODY_BYTES = 1 << 20

# How often the accept loop wakes to notice a shutdown request. stop() cannot return until it does,
# so this is the floor on shutdown latency — the stdlib default of 0.5s is half a second of idling
# on every daemon stop.
_SHUTDOWN_POLL_SEC = 0.02

# How many events one response carries: the page a tail opens on, and the burst cap once it is
# following. A count rather than a time window on purpose — an agent idle since yesterday should
# still open on what it last did, the way `tail -n` does, instead of on an empty page.
_PAGE_EVENTS = 500

# JSON-RPC error codes we use.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602


def _secret_equal(presented: str, known: str) -> bool:
    """Constant-time compare for a secret that arrived over the network.

    `hmac.compare_digest` raises TypeError on a non-ASCII str rather than answering False, and
    both sides of every comparison here come from a request — a header, decoded latin-1, or a
    JSON string. Everything we mint is urlsafe-base64, so a value that will not encode is a
    mismatch by construction; saying so beats letting the handler die on it.
    """
    try:
        return hmac.compare_digest(presented.encode("ascii"), known.encode("ascii"))
    except UnicodeEncodeError:
        return False


class Auth:
    """Maps bearer tokens to sessions: one persistent attended token plus per-pass ephemeral
    tokens minted at spawn and revoked at pass end. Comparison is constant-time; a hidden or
    expired token resolves to None (the endpoint answers 401)."""

    def __init__(self, attended_token: str):
        self._attended = attended_token
        self._pass_tokens: dict = {}  # token -> (Session, expiry_ts)
        self._tail_tickets: dict = {}  # ticket -> expiry_ts
        self._lock = threading.Lock()

    def mint_pass_token(self, tier: str, pass_id: str, expiry_ts: float, scope=None) -> str:
        token = _stdlib_secrets.token_urlsafe(32)
        with self._lock:
            self._pass_tokens[token] = (
                Session(tier=tier, pass_id=pass_id, scope=scope),
                expiry_ts,
            )
        return token

    def revoke_pass_token(self, token: str) -> None:
        with self._lock:
            self._pass_tokens.pop(token, None)

    def rotate_attended(self, new_token: str) -> None:
        """Swap the attended token in place — the rotate route writes the new secret file and
        the live server must honor it without a restart."""
        with self._lock:
            self._attended = new_token

    def mint_tail_ticket(self, now: float | None = None) -> str:
        """A one-shot, short-lived ticket the web tail's URL carries instead of the attended
        token: the address bar and browser history keep whatever rides in a URL, so the real
        token must never be there."""
        ticket = _stdlib_secrets.token_urlsafe(32)
        now = time.time() if now is None else now
        with self._lock:
            self._tail_tickets[ticket] = now + _TAIL_TICKET_TTL_SEC
        return ticket

    def redeem_tail_ticket(self, presented: str | None, now: float | None = None) -> str | None:
        """The attended token in exchange for a live ticket — which this consumes. A second
        redemption, an expired ticket, or an unknown one all answer None."""
        if not presented:
            return None
        now = time.time() if now is None else now
        with self._lock:
            match = None
            for ticket, expiry in self._tail_tickets.items():
                if now <= expiry and _secret_equal(presented, ticket):
                    match = ticket
            for ticket in [t for t, expiry in self._tail_tickets.items() if now > expiry]:
                del self._tail_tickets[ticket]
            if match is None:
                return None
            self._tail_tickets.pop(match, None)
            return self._attended

    def resolve(self, presented: str | None, now: float | None = None) -> Session | None:
        if not presented:
            return None
        with self._lock:
            attended = self._attended
        if _secret_equal(presented, attended):
            return Session(tier="attended", pass_id=None)
        now = time.time() if now is None else now
        with self._lock:
            items = list(self._pass_tokens.items())
        for token, (session, expiry) in items:
            if now <= expiry and _secret_equal(presented, token):
                return session
        return None


class HttpServer:
    """Owns the ThreadingHTTPServer and the auth registry; started after the bus, stopped on
    daemon shutdown. context_factory(session) builds the per-request ToolContext."""

    def __init__(
        self,
        *,
        port: int,
        bus,
        store,
        events_db_path,
        context_factory,
        attended_token: str,
        config=None,
        channels=None,
        host: str = "127.0.0.1",
    ):
        self.bus = bus
        self.store = store
        self.events_db_path = events_db_path
        self.context_factory = context_factory
        self.config = config
        self.channels = channels  # the ChannelManager, so connect can start a provider at runtime
        self.auth = Auth(attended_token)
        self.host = host
        self._httpd = _Server((host, port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.app = self  # the handler reaches shared state via self.server.app
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            args=(_SHUTDOWN_POLL_SEC,),
            name="http-server",
            daemon=True,
        )
        self._thread.start()
        log.info("http server listening on %s:%s", self.host, self.port)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def _localhost_host(header: str | None) -> bool:
    if not header:
        return False  # a request with no Host header is not a browser we trust
    hostname = header.rsplit(":", 1)[0].strip("[]") if ":" in header else header
    return hostname in _LOCALHOST_NAMES


def _localhost_origin(header: str | None) -> bool:
    # An Origin header is only present on cross-origin-capable requests; when present it must be
    # localhost. Its absence (a CLI, an MCP client) is fine.
    if header is None:
        return True
    parsed = urlparse(header)
    return parsed.scheme in ("http", "https") and (parsed.hostname or "") in _LOCALHOST_NAMES


class _Server(ThreadingHTTPServer):
    def server_bind(self) -> None:
        """Bind without asking the network who we are.

        HTTPServer.server_bind calls socket.getfqdn() to fill in a `server_name` we never serve —
        a reverse-DNS lookup that blocks for as long as the resolver takes to give up. It runs
        after `daemon.start` is recorded and before the loop can act on a signal, so where the
        machine has no reverse record for its own address the daemon looks started and answers
        nothing at all, not even SIGTERM.
        """
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # route through logging, not stderr prints
        log.debug("http %s", args)

    @property
    def _app(self) -> HttpServer:
        return self.server.app  # type: ignore[attr-defined]

    # --- request plumbing -----------------------------------------------------------------

    def _guard_localhost(self) -> bool:
        if not _localhost_host(self.headers.get("Host")) or not _localhost_origin(
            self.headers.get("Origin")
        ):
            self._send_json(403, {"error": "forbidden"})
            return False
        return True

    def _read_body(self):
        """The request body, or None once a refusal has already been answered.

        Content-Length is a number a caller chose, and the ticket exchange reads a body before it
        has any credential to check — so it is validated before it is trusted. A negative length
        reaches `rfile.read(-1)`, which blocks until the peer closes and pins one thread of an
        uncapped pool; an enormous one has us allocate whatever was asked for. Neither needs a
        token. The connection closes on a refusal because the body it announced is still unread,
        and on a keep-alive connection those bytes would be parsed as the next request.
        """
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            self._send_json(400, {"error": "invalid Content-Length"})
            return None
        if length > _MAX_BODY_BYTES:
            self.close_connection = True
            self._send_json(413, {"error": "request body too large"})
            return None
        return self.rfile.read(length) if length else b""

    def _bearer(self) -> str | None:
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header[len("Bearer ") :].strip()
        return None

    def _send_json(self, status: int, obj) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --- control-route preamble -------------------------------------------------------------

    def _attended_body(self):
        """The decoded JSON body of an attended-only POST, or None once this has already replied.

        Every control route is the seller reaching their own daemon from their own machine, so
        they share one gate: the attended bearer, and a body that parses. Callers check for None
        rather than falsiness — an empty body is the legitimate `{}`.
        """
        session = self._app.auth.resolve(self._bearer())
        if session is None or session.tier != "attended":
            self._send_json(401, {"error": "unauthorized"})
            return None
        raw = self._read_body()
        if raw is None:
            return None
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._send_json(400, {"error": "invalid json"})
            return None
        if not isinstance(body, dict):
            self._send_json(400, {"error": "body must be a JSON object"})
            return None
        return body

    def _attended_query(self, parsed):
        """The query parameters of an attended-only GET, or None once this has already replied.

        The token rides in the query rather than a header because these are opened by a browser
        (the web tail) as well as called by the CLI.
        """
        qs = parse_qs(parsed.query)
        session = self._app.auth.resolve(qs.get("token", [None])[0])
        if session is None or session.tier != "attended":
            self._send_json(401, {"error": "unauthorized"})
            return None
        return qs

    # --- routing --------------------------------------------------------------------------

    def do_POST(self) -> None:
        if not self._guard_localhost():
            return
        route = urlparse(self.path).path
        if route == "/mcp":
            self._handle_mcp()
        elif route == "/control/enqueue-pass":
            self._handle_enqueue_pass()
        elif route == "/control/connect-telegram":
            self._handle_connect_telegram()
        elif route == "/control/connect-discord":
            self._handle_connect_discord()
        elif route == "/control/settings-decide":
            self._handle_settings_decide()
        elif route == "/control/settings-set":
            self._handle_settings_set()
        elif route == "/control/seller-basics":
            self._handle_seller_basics()
        elif route == "/control/connect-market":
            self._handle_connect_market()
        elif route == "/control/market-login":
            self._handle_market_login()
        elif route == "/control/market-logins":
            self._handle_market_logins()
        elif route == "/control/tail-ticket":
            self._handle_tail_ticket()
        elif route == "/control/tail-exchange":
            self._handle_tail_exchange()
        elif route == "/control/rotate-token":
            self._handle_rotate_token()
        else:
            self._send_json(404, {"error": "not found"})

    def do_GET(self) -> None:
        if not self._guard_localhost():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/mcp":
            self._send_json(405, {"error": "method not allowed"})
        elif parsed.path == "/events.json":
            self._handle_events_json(parsed)
        elif parsed.path == "/control/channel-status":
            self._handle_channel_status(parsed)
        elif parsed.path == "/control/settings-list":
            self._handle_settings_list(parsed)
        elif parsed.path in ("/control/market-login", "/control/market-logins"):
            # POST-only: they navigate the shared browser tab, and a side effect must never be
            # reachable by a URL alone — a top-level navigation sends no bearer and no Origin.
            self._send_json(405, {"error": "method not allowed"})
        elif parsed.path == "/control/seller-basics":
            self._handle_seller_basics_read(parsed)
        elif parsed.path == "/tail":
            self._handle_tail()
        else:
            self._send_json(404, {"error": "not found"})

    # --- MCP ------------------------------------------------------------------------------

    def _handle_mcp(self) -> None:
        session = self._app.auth.resolve(self._bearer())
        if session is None:
            self._send_json(401, {"error": "unauthorized"})
            return
        raw = self._read_body()
        if raw is None:
            return
        try:
            message = json.loads(raw or b"{}")
        except ValueError:
            self._send_json(200, _rpc_error(None, _PARSE_ERROR, "parse error"))
            return
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            self._send_json(200, _rpc_error(None, _INVALID_REQUEST, "invalid request"))
            return

        method = message.get("method")
        msg_id = message.get("id")
        # A notification (no id) — e.g. notifications/initialized — gets no response body.
        if msg_id is None and isinstance(method, str) and method.startswith("notifications/"):
            self._send_empty(202)
            return

        try:
            result = self._dispatch_rpc(method, message.get("params") or {}, session)
        except _RpcError as exc:
            self._send_json(200, _rpc_error(msg_id, exc.code, exc.message))
            return
        self._send_json(200, {"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _dispatch_rpc(self, method, params, session) -> dict:
        if method == "initialize":
            requested = params.get("protocolVersion")
            return {
                "protocolVersion": requested or _DEFAULT_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sellee", "version": __version__},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            tools = [
                {"name": s.name, "description": s.description, "inputSchema": s.input_schema}
                for s in tools_for_tier(session.tier)
            ]
            return {"tools": tools}
        if method == "tools/call":
            return self._tools_call(params, session)
        raise _RpcError(_METHOD_NOT_FOUND, f"method not found: {method}")

    def _tools_call(self, params, session) -> dict:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str):
            raise _RpcError(_INVALID_PARAMS, "tools/call requires a tool name")
        ctx = self._app.context_factory(session)
        try:
            result = dispatch(name, arguments, ctx)
        except (UnknownTool, ToolError) as exc:
            # Tool-level failures are results with isError, not transport errors (MCP spec).
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return {
            "content": [{"type": "text", "text": json.dumps(result, sort_keys=True)}],
            "structuredContent": result,
            "isError": False,
        }

    # --- control --------------------------------------------------------------------------

    def _handle_enqueue_pass(self) -> None:
        body = self._attended_body()
        if body is None:
            return
        pass_type = body.get("type")
        payload = body.get("payload") or {}
        if not isinstance(pass_type, str) or not isinstance(payload, dict):
            self._send_json(400, {"error": "type (string) and payload (object) are required"})
            return
        try:
            passes.validate_payload(pass_type, payload, self._app.store)
        except passes.PassPayloadError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        pass_id = self._app.store.enqueue_pass(pass_type, payload)
        self._app.bus.publish(
            "pass.queued", {"type": pass_type, "payload": payload}, pass_id=pass_id
        )
        self._send_json(200, {"pass_id": pass_id})

    def _handle_connect_telegram(self) -> None:
        # Attended-only: the token arrives here (never argv), the daemon validates it, writes the
        # 0600 secret, arms a bind nonce, and returns the deep link. The token is never echoed and
        # never logged; only bot_username is published.
        from sellee.channel.telegram import bind

        body = self._attended_body()
        if body is None:
            return
        token = body.get("token")
        if not isinstance(token, str):
            self._send_json(400, {"error": "token (string) is required"})
            return
        try:
            result = bind.connect_telegram(self._app.store, self._app.config, token)
        except bind.BindError as exc:
            status = {"bad_token_format": 400, "unauthorized": 401}.get(exc.kind, 502)
            self._send_json(status, {"error": exc.kind, "detail": str(exc)})
            return
        self._app.bus.publish("channel.bind_attempt", {"bot_username": result["bot_username"]})
        # Bring the provider up now if it isn't already — a fresh connect starts the poller at
        # runtime (a reconnect while running is a no-op; the live poller re-reads the new nonce).
        if self._app.channels is not None:
            self._app.channels.register("telegram")
        self._send_json(200, result)

    def _handle_connect_discord(self) -> None:
        from sellee.channel.discord import bind

        body = self._attended_body()
        if body is None:
            return
        token = body.get("token")
        if not isinstance(token, str):
            self._send_json(400, {"error": "token (string) is required"})
            return
        try:
            result = bind.connect_discord(self._app.store, self._app.config, token)
        except bind.BindError as exc:
            status = {"bad_token_format": 400, "unauthorized": 401}.get(exc.kind, 502)
            self._send_json(status, {"error": exc.kind, "detail": str(exc)})
            return
        self._app.bus.publish("channel.bind_attempt", {"bot_username": result["bot_username"]})
        if self._app.channels is not None:
            self._app.channels.register("discord")
        self._send_json(200, result)

    def _handle_settings_decide(self) -> None:
        # Attended CLI door: approve/cancel/undo a change by id. Same trust as the channel buttons —
        # an authenticated surface, a deterministic parse, a deterministic apply — so it works with
        # no channel bound and while paused (seller-initiated control).
        from sellee import settings

        body = self._attended_body()
        if body is None:
            return
        action = body.get("action")
        change_id = body.get("change_id")
        if action not in settings.TEXT_VERBS or not isinstance(change_id, str):
            self._send_json(
                400, {"error": "action (approve|cancel|undo) and change_id are required"}
            )
            return
        result = settings.decide(
            self._app.store, self._app.bus, change_id=change_id, decision=action, decided_via="cli"
        )
        self._send_json(200, result)

    def _handle_settings_set(self) -> None:
        # The installer's and the CLI's way to set a setting outright. Typing it at your own
        # terminal is the consent the approve door exists to collect, so there is no round-trip —
        # but validation is the propose path's, seller-state check included, and the prior value
        # is recorded so Undo still works.
        from sellee import settings

        body = self._attended_body()
        if body is None:
            return
        key = body.get("key")
        if not isinstance(key, str) or "value" not in body:
            self._send_json(400, {"error": "key (string) and value are required"})
            return
        try:
            result = settings.set_now(
                self._app.store, self._app.bus, key=key, raw_value=body["value"]
            )
        except settings.SettingError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(200, result)

    def _handle_seller_basics(self) -> None:
        # Where the seller sells, which provisioning needs before it can ask for a key. The store
        # is the daemon's to write, so the installer asks through this door rather than opening
        # the database behind a running process.
        from sellee.tools.seller import BasicsError, validate_basics

        body = self._attended_body()
        if body is None:
            return
        try:
            basics = validate_basics(body)
        except BasicsError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        if not basics:
            self._send_json(400, {"error": "provide at least one of region, currency, timezone"})
            return
        current = self._app.store.get_seller_config_section("basics") or {}
        merged = {**current, **basics}
        self._app.store.set_seller_config_section("basics", merged)
        self._app.bus.publish("seller.basics_set", {"region": merged.get("region")})
        self._send_json(200, {"status": "written", "basics": merged})

    def _handle_seller_basics_read(self, parsed) -> None:
        # The read half, so a caller that needs the region asks the daemon for it rather than
        # opening the database a live process owns.
        if self._attended_query(parsed) is None:
            return
        basics = self._app.store.get_seller_config_section("basics") or {}
        self._send_json(200, {"basics": basics})

    def _handle_connect_market(self) -> None:
        # Open the marketplace in the agent's own Chrome so the seller can sign in there. We
        # never sign in for them, and nothing about their session is recorded: the cookies in
        # that profile are the truth, and the probe re-derives the answer whenever it is asked.
        from sellee import settings

        body = self._attended_body()
        if body is None:
            return
        market = body.get("market")
        adapter = self._market_adapter(market)
        if adapter is None:
            return
        if self._app.store.active_passes_of_types(_BROWSER_PASS_TYPES):
            # Unlike Chrome being closed — which this route exists to fix — a pass mid-drive
            # owns the tab. Navigating it now would pull the page out from under a half-filled
            # composer, and the seller asked to sign in, not to lose a listing.
            self._send_json(
                409, {"error": "browser_busy", "detail": "a pass is using the browser right now"}
            )
            return
        try:
            state, url = self._open_and_probe(adapter, bring_tab_forward=True)
        except _BrowserDown as exc:
            self._send_json(503, {"error": "browser_unavailable", "detail": str(exc)})
            return
        self._app.bus.publish("browser.login", {"market": adapter.market, "state": state})
        # The OS-level raise happens in the CLI (the seller's own frontmost terminal, where
        # activation is honored), but whether they want it is a setting only this side can read.
        raise_window = bool(settings.get(self._app.store, "raise_browser"))
        self._send_json(
            200,
            {"market": adapter.market, "url": url, "state": state, "raise_window": raise_window},
        )

    def _handle_market_login(self) -> None:
        # A read in intent, but it navigates the shared tab, so it takes the same POST +
        # Bearer gate as every other route that touches the browser (its only caller is the
        # CLI). It never opens a window and never elbows a pass off the tab; the connect route
        # above is the one that may start Chrome, because being asked to open a marketplace is
        # being asked for a window.
        body = self._attended_body()
        if body is None:
            return
        adapter = self._market_adapter(body.get("market"))
        if adapter is None:
            return
        blocked = self._probe_blocked()
        if blocked:
            self._send_json(200, {"market": adapter.market, "state": "unknown", "detail": blocked})
            return
        try:
            state, url = self._open_and_probe(adapter)
        except _BrowserDown as exc:
            self._send_json(503, {"error": "browser_unavailable", "detail": str(exc)})
            return
        self._send_json(200, {"market": adapter.market, "url": url, "state": state})

    def _probe_blocked(self):
        """Why a login probe must not run right now, or "" when it may.

        Two reasons, both about not surprising the seller: Chrome being closed (probing acquires
        the browser, which starts it — a status read that opens a window is not a status read),
        and a browser pass already driving the one tab we would navigate.
        """
        from sellee import config as config_module
        from sellee.browser import chrome

        cfg = self._app.config or config_module.load()
        if not chrome.is_ready(chrome.resolve_port(cfg.chrome_cdp_port)):
            return "Chrome isn't running"
        if self._app.store.active_passes_of_types(_BROWSER_PASS_TYPES):
            return "a pass is using the browser"
        return ""

    def _handle_market_logins(self) -> None:
        """Every marketplace the seller enabled, and — only if Chrome is already up — whether
        they are still signed in to each.

        Both halves are answered here because both depend on state this side owns: the enabled
        list comes from the store crossed with the seller's region, and whether probing is
        allowed at all comes from Chrome's port and the pass queue (see `_probe_blocked`).
        POST for the same reason as market-login: the probe navigates the shared tab.
        """
        from sellee import settings
        from sellee.browser import markets as market_adapters

        if self._attended_body() is None:
            return
        enabled = settings.crosslist_markets(self._app.store)
        # The *reason* travels with the answer: "Chrome isn't running" and "a pass is using the
        # browser" both stop a probe, but a reader told the first while watching Chrome run a
        # publish learns to distrust the report.
        blocked = self._probe_blocked()

        results = []
        if not blocked:
            for market in enabled:
                adapter = market_adapters.get_adapter(market)
                if adapter is None:
                    continue
                try:
                    state, _url = self._open_and_probe(adapter)
                except _BrowserDown as exc:
                    results.append({"market": market, "state": "unknown", "detail": str(exc)})
                    continue
                results.append({"market": market, "state": state})
        self._send_json(200, {"enabled": enabled, "blocked": blocked, "markets": results})

    def _market_adapter(self, market):
        """The adapter for a market id, or None once this has replied with why there isn't one."""
        from sellee.browser import markets as market_adapters

        adapter = market_adapters.get_adapter(market) if isinstance(market, str) else None
        if adapter is None:
            supported = ", ".join(market_adapters.supported_markets()) or "(none)"
            self._send_json(
                400, {"error": f"no marketplace {market!r} to sign in to — supported: {supported}"}
            )
            return None
        return adapter

    def _open_and_probe(self, adapter, *, bring_tab_forward=False):
        """Put the market's own page in front of the seller and read back whether they are in.

        Held exclusively across the navigate and the probe: the read lane shares this one tab,
        and a probe that ran after it moved on would be answering about a different page.

        `bring_tab_forward` selects the tab within the agent's window and is set only by the
        connect route: being asked to open a marketplace is being asked for a window, while a
        read probe that reordered tabs would elbow the seller mid-browse.
        """
        from sellee import marketplaces
        from sellee.browser.client import BrowserError

        region = self._app.store.seller_region()
        url = marketplaces.market_home(adapter.market, region)
        if url is None:
            raise _BrowserDown(
                f"{marketplaces.display_name(adapter.market)} has no site for "
                f"{region or 'an unset region'}"
            )
        session = Session(tier="attended", pass_id=None)
        ctx = self._app.context_factory(session)
        try:
            client = ctx.browser_factory()
            with client.exclusive():
                client.navigate(url)
                if bring_tab_forward:
                    try:
                        client.ensure_frontmost(url)
                    except Exception:
                        # A tab that won't come forward must never turn a working sign-in page
                        # into a 503. But bringing one forward selects a tab before it can check
                        # which tab it got, and a select repoints every later call — so a failure
                        # can leave the probe below reading the seller's own page and reporting a
                        # login state about it. Navigating again re-opens a tab of ours and puts
                        # the market back in it, which is what the probe has to be answering about.
                        log.debug("could not bring the connect tab forward", exc_info=True)
                        client.navigate(url)
                answer = client.evaluate(adapter.login_js) or {}
        except BrowserError as exc:
            raise _BrowserDown(str(exc)) from exc
        state = answer.get("state")
        return (state if state in ("logged_in", "logged_out") else "unknown"), url

    def _handle_settings_list(self, parsed) -> None:
        from sellee import settings

        if self._attended_query(parsed) is None:
            return
        self._send_json(
            200,
            {
                "pending": settings.pending_view(self._app.store),
                "settings": settings.describe(self._app.store),
            },
        )

    def _handle_channel_status(self, parsed) -> None:
        # The channel row is a singleton (one adapter bound at a time — see store.arm_bind), so
        # whichever adapter it names answers channel_status.
        from sellee.channel.discord import bind as discord_bind
        from sellee.channel.telegram import bind as telegram_bind

        if self._attended_query(parsed) is None:
            return
        adapter = self._app.store.get_channel()["adapter"]
        bind_module = discord_bind if adapter == "discord" else telegram_bind
        self._send_json(200, bind_module.channel_status(self._app.store))

    def _handle_tail_ticket(self) -> None:
        # The CLI trades its bearer for a one-shot ticket to put in the tail URL it opens.
        if self._attended_body() is None:
            return
        self._send_json(200, {"ticket": self._app.auth.mint_tail_ticket()})

    def _handle_tail_exchange(self) -> None:
        # The tail page trades the ticket back for the attended token it polls with. No bearer
        # gate — the page has no token yet, that is the point — but the localhost Host/Origin
        # guard already ran, and the ticket is single-use and minutes-lived. This is the one route
        # that reads a body with no credential behind it, which is why _read_body validates the
        # announced length rather than trusting it.
        raw = self._read_body()
        if raw is None:
            return
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._send_json(400, {"error": "invalid json"})
            return
        ticket = body.get("ticket") if isinstance(body, dict) else None
        token = self._app.auth.redeem_tail_ticket(ticket if isinstance(ticket, str) else None)
        if token is None:
            self._send_json(401, {"error": "unauthorized"})
            return
        self._send_json(200, {"token": token})

    def _handle_rotate_token(self) -> None:
        # Mint, persist, and swap in a fresh attended token. The old token dies here — every
        # place it was ever pasted or printed stops working.
        from sellee import secrets

        if self._attended_body() is None:
            return
        new_token = secrets.rotate_mcp_token()
        self._app.auth.rotate_attended(new_token)
        self._send_json(200, {"status": "rotated"})

    # --- web tail -------------------------------------------------------------------------

    def _handle_events_json(self, parsed) -> None:
        qs = self._attended_query(parsed)
        if qs is None:
            return
        after_seq = _int_or_none(qs.get("after_seq", [None])[0])
        pass_id = qs.get("pass", [None])[0]
        # An optional narrowing, not the default: with no after_seq this is a page opening, and it
        # opens on the newest events whatever their age. Nonsense or non-positive means "no window".
        since_sec = _int_or_none(qs.get("since_sec", [None])[0])
        since_ts = time.time() - since_sec if since_sec and since_sec > 0 else None
        seeding = after_seq is None
        conn = connect_reader(self._app.events_db_path)
        try:
            # The ceiling is read before the rows so nothing arriving mid-request is skipped: a
            # later event gets a higher seq and is simply picked up by the next poll.
            ceiling = latest_seq(conn)
            events = query_events(
                conn,
                after_seq=after_seq,
                upto_seq=ceiling,
                since_ts=since_ts,
                pass_id=pass_id,
                # The routine tier never reaches this page. It is ~99% of the volume on a busy
                # install and none of it is what someone opens a tail to read; `logs --all` is
                # where that tier is answered.
                exclude_kinds=routine_kinds(),
                limit=_PAGE_EVENTS,
                newest=seeding,
            )
        finally:
            conn.close()
        rows = [event_to_wire(e) for e in events]
        # The cursor advances to the ceiling, not to the last row returned: everything below it has
        # been considered, and the tier dropped above leaves gaps that would otherwise be rescanned
        # on every poll for as long as the page stays open.
        last_seq = ceiling or (after_seq or 0)
        self._send_json(200, {"events": rows, "last_seq": last_seq})

    def _handle_tail(self) -> None:
        # Read per request rather than at import: the page is a packaged asset, and a versioned
        # install is an unpacked source tree, so editing it and reloading is the whole dev loop.
        page = _TAIL_PAGE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)


class _BrowserDown(Exception):
    """The browser could not be driven for a connect/probe request — answered as a 503 with the
    reason, so the CLI can print the by-hand hint instead of pretending the market is signed out."""


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _rpc_error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _int_or_none(raw):
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
