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
    headers = {key.lower(): value for key, value in handler.headers.items()}
    method = body.get("method")
    params = body.get("params") or {}
    log.requests.append({"method": method, "headers": headers, "params": params})
    supported = ((MODERN,) if log.era == "modern" else ()) + LEGACY_VERSIONS
    version = headers.get("mcp-protocol-version", "")
    if (version or "2025-03-26") not in supported:
        # go-sdk rejects an unknown version header in plain text, before reading the body.
        _plain(handler, 400, "Bad Request: Unsupported protocol version")
        return True
    if version == MODERN:
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
