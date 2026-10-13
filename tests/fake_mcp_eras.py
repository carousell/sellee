"""What carousell.ai's MCP server answers before any tool runs: `legacy` as go-sdk v1.6.1
stateless (bazaar today), `modern` as a dual-era go-sdk v1.8.0 server with Stateless set."""

from __future__ import annotations

import json

MODERN = "2026-07-28"
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
_NAMED_METHODS = ("tools/call", "resources/read", "prompts/get")


class EraLog:
    """One fake server's era, and every MCP request it answered or passed on to a tool."""

    def __init__(self, era: str = "legacy"):
        self.era = era
        # Legacy only: session ids `initialize` hands out in turn, and ids the server has dropped.
        self.sessions: list = []
        self.dropped: set = set()
        # Legacy only: refuse calls on a session until `notifications/initialized`, as a strict
        # server does; go-sdk does not check. The empty-string session stands for "no session id".
        self.require_ready = False
        self.ready: set = set()
        # Legacy only: accept any version header, as many legacy servers do, so an unknown method
        # such as server/discover is answered with a -32601 error at HTTP 200.
        self.lenient = False
        # Modern only: no server/discover, answered as the spec says: HTTP 404 with -32601.
        self.no_discover = False
        # Modern only: refuse every legacy version, as a server that dropped the legacy era does.
        self.modern_only = False
        # Refuse every tools/call at HTTP 400, before any tool runs: a JSON-RPC error code to refuse
        # a modern call with, or "plain" for a plain-text 400 in either era.
        self.refuse_calls: object = 0
        # Modern only: reject an unsupported version header with JSON -32022, as the spec asks,
        # rather than go-sdk's plain-text 400.
        self.json_rejections = False
        # The tools/call requests that reached a tool, as opposed to ones refused before it.
        self.executed = 0
        # Modern only: a JSON-RPC error code to answer server/discover with, at HTTP 200.
        self.discover_error = 0
        self.requests: list = []

    def count(self, method: str) -> int:
        return sum(1 for request in self.requests if request["method"] == method)

    def calls(self) -> list:
        return [request for request in self.requests if request["method"] == "tools/call"]


def answer(handler, body: dict, log: EraLog) -> bool:
    """Write the server's answer when the protocol layer settles the request; False when the request
    reaches the tool handler."""
    settled = _answer(handler, body, log)
    if not settled and body.get("method") == "tools/call":
        log.executed += 1
    return settled


def _answer(handler, body: dict, log: EraLog) -> bool:
    headers = {key.lower(): value for key, value in handler.headers.items()}
    method = body.get("method")
    params = body.get("params") or {}
    log.requests.append({"method": method, "headers": headers, "params": params})
    legacy = () if (log.era == "modern" and log.modern_only) else LEGACY_VERSIONS
    supported = ((MODERN,) if log.era == "modern" else ()) + legacy
    version = headers.get("mcp-protocol-version", "")
    if (version or "2025-03-26") not in supported and not (log.lenient and log.era == "legacy"):
        if log.json_rejections:
            data = {"supported": list(supported), "requested": version or "2025-03-26"}
            error = {"code": -32022, "message": "unsupported protocol version", "data": data}
            _json(handler, 400, {"jsonrpc": "2.0", "id": body.get("id"), "error": error})
        else:
            # go-sdk, v1.6.1 and v1.8.0 alike, rejects a version header in plain text.
            listed = ",".join(supported)
            _plain(
                handler,
                400,
                f"Bad Request: Unsupported protocol version (supported versions: {listed})",
            )
        return True
    if method == "tools/call" and log.refuse_calls == "plain":
        _plain(handler, 400, "Bad Request")
        return True
    if version == MODERN and log.era == "modern":
        return _modern(handler, body, headers, method, params, supported, log)
    session = headers.get("mcp-session-id", "")
    if session and session in log.dropped:
        _plain(handler, 404, "session not found")
        return True
    if method == "initialize":
        requested = params.get("protocolVersion")
        negotiated = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        extra = {"Mcp-Session-Id": log.sessions.pop(0)} if log.sessions else {}
        result = {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "bazaar", "version": "0.1.0"},
        }
        _json(handler, 200, {"jsonrpc": "2.0", "id": body.get("id"), "result": result}, extra)
        return True
    if method == "notifications/initialized" and "id" not in body:
        log.ready.add(session)
        _empty(handler, 202)
        return True
    if method == "server/discover":
        _error(handler, 200, body, -32601, "method not found")
        return True
    if log.require_ready and session not in log.ready:
        _error(handler, 200, body, -32600, "method invalid during session initialization")
        return True
    return False


def _modern(handler, body, headers, method, params, supported, log) -> bool:
    if headers.get("mcp-method") != method:
        _error(handler, 400, body, -32020, "header mismatch: Mcp-Method")
        return True
    if method in _NAMED_METHODS and headers.get("mcp-name") != params.get("name"):
        _error(handler, 400, body, -32020, "header mismatch: Mcp-Name")
        return True
    meta = params.get("_meta") or {}
    if meta.get(META_VERSION) != MODERN or not isinstance(meta.get(META_CAPABILITIES), dict):
        _error(handler, 200, body, -32602, "missing or invalid _meta")
        return True
    if method == "tools/call" and isinstance(log.refuse_calls, int) and log.refuse_calls:
        _error(handler, 400, body, log.refuse_calls, "header mismatch")
        return True
    if method == "server/discover" and log.no_discover:
        _error(handler, 404, body, -32601, "method not found")
        return True
    if method == "server/discover" and log.discover_error:
        _error(handler, 200, body, log.discover_error, "missing required client capabilities")
        return True
    if method == "server/discover":
        result = {"supportedVersions": list(supported), "capabilities": {"tools": {}}}
        _json(handler, 200, {"jsonrpc": "2.0", "id": body.get("id"), "result": result})
        return True
    if method == "initialize":
        _error(handler, 200, body, -32601, "initialize is not supported in the new protocol")
        return True
    return False


def _error(handler, status, body, code, message):
    error = {"code": code, "message": message}
    _json(handler, status, {"jsonrpc": "2.0", "id": body.get("id"), "error": error})


def _json(handler, status, obj, extra=None):
    payload = json.dumps(obj).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    for name, value in (extra or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(payload)


def _empty(handler, status):
    handler.send_response(status)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _plain(handler, status, text):
    payload = text.encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)
