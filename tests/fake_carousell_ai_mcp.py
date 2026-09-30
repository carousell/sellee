"""A fake carousell.ai seller MCP server holding relay threads, as bazaar's list_threads and
get_thread serve them: proto field names, RFC 3339 times, and at-least-once paging."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class FakeRelay:
    """The threads and messages bazaar would hold for one seller."""

    def __init__(self):
        self.threads: dict = {}
        self.messages: dict = {}
        self.down = False
        self.calls: list = []
        # Resend the last thread already paged past, as bazaar's 30-second overlap does.
        self.repeat_tail = True
        self._clock = 1_800_000_000.0

    def tick(self) -> float:
        self._clock += 1.0
        return self._clock

    def add_thread(self, thread_id, *, listing_id, email=None, name="Alice"):
        self.threads[thread_id] = {
            "id": thread_id,
            "listing_id": listing_id,
            "listing_title": "Teak lamp",
            "thread_email": email or f"{thread_id}@reply.carousell.ai",
            "buyer_name": name,
            "buyer_blocked": False,
            "updated": self.tick(),
        }
        self.messages[thread_id] = []

    def add_message(self, thread_id, msg_id, author, text, *, pending_send=False, client_id=""):
        ts = self.tick()
        self.messages[thread_id].append(
            {
                "id": msg_id,
                "author": author,
                "text": text,
                "sent_at": rfc3339(ts),
                "client_message_id": client_id if author == "agent" else "",
                "pending_send": pending_send,
            }
        )
        self.threads[thread_id]["updated"] = ts

    def block(self, thread_id):
        self.threads[thread_id]["buyer_blocked"] = True
        self.threads[thread_id]["updated"] = self.tick()

    def _summary(self, thread_id) -> dict:
        t = self.threads[thread_id]
        last = self.messages[thread_id][-1]
        return {
            **{k: v for k, v in t.items() if k != "updated"},
            "last_message_at": last["sent_at"],
            "last_message_author": last["author"],
        }

    def list_threads(self, args: dict) -> dict:
        after = float(args.get("cursor") or 0)
        limit = int(args.get("limit") or 50)
        rows = sorted(
            (t for t in self.threads.values() if self.messages[t["id"]]),
            key=lambda t: t["updated"],
        )
        page = [t for t in rows if t["updated"] > after][:limit]
        if self.repeat_tail and after:
            page = [t for t in rows if t["updated"] == after] + page
            page = page[:limit]
        cursor = str(page[-1]["updated"]) if page else (args.get("cursor") or "")
        return {"threads": [self._summary(t["id"]) for t in page], "next_cursor": cursor}

    def get_thread(self, args: dict) -> dict:
        thread_id = args["id"]
        return {"thread": self._summary(thread_id), "messages": list(self.messages[thread_id])}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        relay: FakeRelay = self.server.relay  # type: ignore[attr-defined]
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if relay.down:
            self._send(503, {"error": "unavailable"})
            return
        if body.get("method") != "tools/call":
            self._send(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": {}})
            return
        name = body["params"]["name"]
        args = body["params"].get("arguments") or {}
        relay.calls.append((name, args))
        handler = {"list_threads": relay.list_threads, "get_thread": relay.get_thread}.get(name)
        if handler is None:
            result = {"isError": True, "content": [{"type": "text", "text": f"no tool {name}"}]}
        else:
            result = {"structuredContent": handler(args)}
        self._send(200, {"jsonrpc": "2.0", "id": body["id"], "result": result})

    def _send(self, status, obj):
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve(relay: FakeRelay):
    """Start the fake on a free port; returns (base_url, shutdown)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.relay = relay  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def shutdown():
        server.shutdown()
        server.server_close()

    return f"http://127.0.0.1:{server.server_port}", shutdown
