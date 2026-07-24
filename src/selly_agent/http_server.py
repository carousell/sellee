"""The daemon's one localhost HTTP server: MCP endpoint, web tail, and the pass-control route.

Bound to 127.0.0.1 only. Three surfaces share it:

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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from selly_agent import __version__
from selly_agent.db import connect_reader
from selly_agent.events import query_events
from selly_agent.tools.registry import Session, ToolError, UnknownTool, dispatch, tools_for_tier

log = logging.getLogger(__name__)

_LOCALHOST_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
_DEFAULT_PROTOCOL_VERSION = "2025-06-18"

# JSON-RPC error codes we use.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602


class Auth:
    """Maps bearer tokens to sessions: one persistent attended token plus per-pass ephemeral
    tokens minted at spawn and revoked at pass end. Comparison is constant-time; a hidden or
    expired token resolves to None (the endpoint answers 401)."""

    def __init__(self, attended_token: str):
        self._attended = attended_token
        self._pass_tokens: dict = {}  # token -> (Session, expiry_ts)
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

    def resolve(self, presented: str | None, now: float | None = None) -> Session | None:
        if not presented:
            return None
        if hmac.compare_digest(presented, self._attended):
            return Session(tier="attended", pass_id=None)
        now = time.time() if now is None else now
        with self._lock:
            items = list(self._pass_tokens.items())
        for token, (session, expiry) in items:
            if now <= expiry and hmac.compare_digest(presented, token):
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
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.app = self  # the handler reaches shared state via self.server.app
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="http-server", daemon=True
        )
        self._thread.start()
        log.info("http server listening on 127.0.0.1:%s", self.port)

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

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
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
        try:
            message = json.loads(self._read_body() or b"{}")
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
                "serverInfo": {"name": "selly-agent", "version": __version__},
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
        session = self._app.auth.resolve(self._bearer())
        if session is None or session.tier != "attended":
            self._send_json(401, {"error": "unauthorized"})
            return
        try:
            body = json.loads(self._read_body() or b"{}")
        except ValueError:
            self._send_json(400, {"error": "invalid json"})
            return
        pass_type = body.get("type")
        payload = body.get("payload") or {}
        if not isinstance(pass_type, str) or not isinstance(payload, dict):
            self._send_json(400, {"error": "type (string) and payload (object) are required"})
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
        from selly_agent.channel.telegram import bind

        session = self._app.auth.resolve(self._bearer())
        if session is None or session.tier != "attended":
            self._send_json(401, {"error": "unauthorized"})
            return
        try:
            body = json.loads(self._read_body() or b"{}")
        except ValueError:
            self._send_json(400, {"error": "invalid json"})
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

    def _handle_channel_status(self, parsed) -> None:
        from selly_agent.channel.telegram import bind

        qs = parse_qs(parsed.query)
        session = self._app.auth.resolve(qs.get("token", [None])[0])
        if session is None or session.tier != "attended":
            self._send_json(401, {"error": "unauthorized"})
            return
        self._send_json(200, bind.channel_status(self._app.store))

    # --- web tail -------------------------------------------------------------------------

    def _handle_events_json(self, parsed) -> None:
        qs = parse_qs(parsed.query)
        token = qs.get("token", [None])[0]
        session = self._app.auth.resolve(token)
        if session is None or session.tier != "attended":
            self._send_json(401, {"error": "unauthorized"})
            return
        after_seq = _int_or_none(qs.get("after_seq", [None])[0])
        pass_id = qs.get("pass", [None])[0]
        conn = connect_reader(self._app.events_db_path)
        try:
            events = query_events(conn, after_seq=after_seq, pass_id=pass_id, limit=500)
        finally:
            conn.close()
        rows = [
            {"seq": e.seq, "ts": e.ts, "pass_id": e.pass_id, "kind": e.kind, "payload": e.payload}
            for e in events
        ]
        last_seq = rows[-1]["seq"] if rows else (after_seq or 0)
        self._send_json(200, {"events": rows, "last_seq": last_seq})

    def _handle_tail(self) -> None:
        page = _TAIL_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)


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


# A single static page that polls events.json using the token from its own URL query.
_TAIL_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>selly-agent tail</title>
<style>body{font:13px/1.5 monospace;margin:1rem;background:#111;color:#ddd}
.k{color:#7cf}.p{color:#888}pre{white-space:pre-wrap;word-break:break-all}</style></head>
<body><h3>selly-agent event tail</h3><pre id="log"></pre>
<script>
const params = new URLSearchParams(location.search);
const token = params.get("token") || "";
let after = 0;
async function poll(){
  try{
    const r = await fetch(`events.json?token=${encodeURIComponent(token)}&after_seq=${after}`);
    if(r.ok){
      const d = await r.json();
      for(const e of d.events){
        const line = document.createElement("div");
        line.innerHTML = `<span class=p>${new Date(e.ts*1000).toLocaleTimeString()}</span> `
          + `<span class=k>${e.kind}</span> `
          + `<span class=p>pass=${e.pass_id||"-"}</span> ${JSON.stringify(e.payload)}`;
        document.getElementById("log").appendChild(line);
      }
      after = d.last_seq;
    }
  }catch(e){}
  setTimeout(poll, 1000);
}
poll();
</script></body></html>
"""
