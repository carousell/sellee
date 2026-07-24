"""`selly-agent mcp-proxy` — the stdio shim for harnesses that speak MCP only over stdio.

Newline-delimited JSON-RPC on stdin/stdout, one HTTP round-trip per line to the daemon's
POST /mcp. This is the null-cost bridge for Codex / any stdio-only client: the daemon's HTTP
MCP server stays the single implementation. The attended token comes from the config-dir
secret; the port from config. A notification (a 202 with no body) produces no stdout line.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from selly_agent import config, secrets

_TIMEOUT_SEC = 120.0
_INTERNAL_ERROR = -32603


def forward(body: bytes, endpoint: str, token: str, *, timeout: float = _TIMEOUT_SEC) -> str | None:
    """POST one JSON-RPC message to the daemon and return its response text (None for an empty
    202-style response). A transport failure becomes a JSON-RPC error carrying the request id."""
    req = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            # a localhost origin so the server's DNS-rebinding guard is satisfied
            "Origin": "http://127.0.0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return raw or None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return raw or None
    except (urllib.error.URLError, OSError) as exc:
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": _request_id(body),
                "error": {"code": _INTERNAL_ERROR, "message": f"daemon unreachable: {exc}"},
            }
        )


def _request_id(body: bytes):
    try:
        return json.loads(body).get("id")
    except (ValueError, AttributeError):
        return None


def run_loop(stdin, stdout, endpoint: str, token: str) -> int:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        response = forward(line.encode("utf-8"), endpoint, token)
        if response is not None:
            stdout.write(response + "\n")
            stdout.flush()
    return 0


def main(args) -> int:
    token = secrets.read_mcp_token()
    if not token:
        print(
            "selly-agent: no MCP token found — start the daemon first (selly-agent daemon run)",
            file=sys.stderr,
        )
        return 1
    port = config.load().http_port
    endpoint = f"http://127.0.0.1:{port}/mcp"
    return run_loop(sys.stdin, sys.stdout, endpoint, token)
