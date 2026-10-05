"""A fake carousell.ai seller MCP server holding relay threads and registration mail, as bazaar
serves them: proto field names, RFC 3339 times, and at-least-once paging."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class ToolFailure(Exception):
    """A tool error, carrying the text bazaar's MCP transport would return for it."""


class FakeRelay:
    """The threads and messages bazaar would hold for one seller."""

    def __init__(self):
        self.threads: dict = {}
        self.messages: dict = {}
        self.down = False
        # Threads whose get_thread fails, as a thread bazaar cannot serve would.
        self.broken: set = set()
        # Resend the last thread already paged past, as bazaar's 30-second overlap does.
        self.repeat_tail = True
        self._clock = 1_800_000_000.0
        # One reply_to_thread outcome per call ("ok" once empty): http503, internal store nothing;
        # pending (not sent yet), lost (answer lost as a 5xx), slow and no_id store the reply first.
        self.reply_script: list = []
        self.reply_calls: list = []
        self.slow_sec = 0.0
        self._replies = 0
        # Registration mail, its correspondents, and the replies bazaar stored and actually sent.
        self.mail: list = []
        self.correspondents: set = set()
        self.registration_replies: dict = {}
        self.registration_sent: list = []
        self.registration_calls: list = []
        # One send_registration_reply outcome per call ("ok" once empty): unavailable and http503
        # send nothing; lost sends, then answers as a 5xx.
        self.registration_script: list = []

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

    def finish_send(self, thread_id, msg_id):
        """An agent reply's owed send completes. bazaar moves no thread stamp for this."""
        for message in self.messages[thread_id]:
            if message["id"] == msg_id:
                message["pending_send"] = False

    def block(self, thread_id):
        self.threads[thread_id]["buyer_blocked"] = True
        self.threads[thread_id]["updated"] = self.tick()

    def reply_to_thread(self, args: dict) -> dict:
        """Store an agent reply once per client_message_id, as bazaar does."""
        self.reply_calls.append(dict(args))
        step = self.reply_script.pop(0) if self.reply_script else "ok"
        if step == "internal":
            raise ToolFailure("internal server error")
        thread_id = args["id"]
        if self.threads[thread_id]["buyer_blocked"]:
            raise ToolFailure("the buyer is blocked")
        existing = next(
            (
                m
                for m in self.messages[thread_id]
                if m["client_message_id"] == args["client_message_id"]
            ),
            None,
        )
        if existing is None:
            self._replies += 1
            msg_id = f"r{self._replies}"
            self.add_message(
                thread_id,
                msg_id,
                "agent",
                args["text"],
                pending_send=step == "pending",
                client_id=args["client_message_id"],
            )
        else:
            msg_id = existing["id"]
        if step == "lost":
            raise ToolFailure("internal server error")
        if step == "slow":
            time.sleep(self.slow_sec)
        if step == "no_id":
            return {}
        return {"message_id": msg_id}

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

    def add_mail(self, mail_id, *, from_email, subject, text, from_domain=None, from_name=""):
        """Mail to the seller's registration address; its sender becomes a correspondent."""
        from_email = from_email.lower()
        self.mail.append(
            {
                "id": mail_id,
                "from_email": from_email,
                "from_name": from_name,
                "from_domain": from_domain or from_email.rsplit("@", 1)[-1],
                "subject": subject,
                "text": text,
                "received": self.tick(),
            }
        )
        self.correspondents.add(from_email)

    def list_registration_mail(self, args: dict) -> dict:
        after = float(args.get("cursor") or 0)
        limit = int(args.get("limit") or 50)
        rows = sorted(self.mail, key=lambda m: (m["received"], m["id"]))
        page = [m for m in rows if m["received"] > after][:limit]
        if self.repeat_tail and after:
            page = ([m for m in rows if m["received"] == after] + page)[:limit]
        cursor = str(page[-1]["received"]) if page else (args.get("cursor") or "")
        mail = [
            {
                **{k: v for k, v in m.items() if k != "received"},
                "received_at": rfc3339(m["received"]),
            }
            for m in page
        ]
        return {"mail": mail, "next_cursor": cursor}

    def send_registration_reply(self, args: dict) -> dict:
        """Send once per client_message_id, only to a correspondent, as bazaar does."""
        self.registration_calls.append(dict(args))
        step = self.registration_script.pop(0) if self.registration_script else "ok"
        subject = args.get("subject") or ""
        if not subject.strip() or "\n" in subject or "\r" in subject:
            raise ToolFailure("subject must be one line")
        to = args["to"].lower()
        stored = self.registration_replies.get(args["client_message_id"])
        if stored is None:
            stored = {"to": to, "text": args["text"], "sent_at": ""}
            if to not in self.correspondents:
                raise ToolFailure("to has never mailed the seller's registration address")
            self.registration_replies[args["client_message_id"]] = stored
        elif stored["text"] != args["text"]:
            raise ToolFailure("client_message_id already names a different reply")
        if not stored["sent_at"]:
            if stored["to"] not in self.correspondents:
                raise ToolFailure("to has never mailed the seller's registration address")
            if step == "unavailable":
                raise ToolFailure("the reply was not sent; retry with the same client_message_id")
            stored["sent_at"] = rfc3339(self.tick())
            self.registration_sent.append({**args, "to": stored["to"]})
        if step == "lost":
            raise ToolFailure("internal server error")
        return {"sent_at": stored["sent_at"]}

    def get_thread(self, args: dict) -> dict:
        thread_id = args["id"]
        if thread_id in self.broken:
            raise LookupError(thread_id)
        return {"thread": self._summary(thread_id), "messages": list(self.messages[thread_id])}


_TOOLS = (
    "list_threads",
    "get_thread",
    "reply_to_thread",
    "list_registration_mail",
    "send_registration_reply",
)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        relay: FakeRelay = self.server.relay  # type: ignore[attr-defined]
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if relay.down:
            self._send(503, {"error": "unavailable"})
            return
        if body.get("method") == "tools/list":
            tools = [{"name": n} for n in _TOOLS]
            self._send(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": {"tools": tools}})
            return
        if body.get("method") != "tools/call":
            self._send(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": {}})
            return
        name = body["params"]["name"]
        args = body["params"].get("arguments") or {}
        if name == "reply_to_thread" and relay.reply_script[:1] in (["http503"], ["http429"]):
            status = int(relay.reply_script.pop(0)[4:])
            relay.reply_calls.append(dict(args))
            self._send(status, {"error": "unavailable"})
            return
        if name == "send_registration_reply" and relay.registration_script[:1] == ["http503"]:
            relay.registration_script.pop(0)
            relay.registration_calls.append(dict(args))
            self._send(503, {"error": "unavailable"})
            return
        handler = {
            "list_threads": relay.list_threads,
            "get_thread": relay.get_thread,
            "reply_to_thread": relay.reply_to_thread,
            "list_registration_mail": relay.list_registration_mail,
            "send_registration_reply": relay.send_registration_reply,
        }.get(name)
        if handler is None:
            result = {"isError": True, "content": [{"type": "text", "text": f"no tool {name}"}]}
        else:
            try:
                result = {"structuredContent": handler(args)}
            except LookupError:
                result = {"isError": True, "content": [{"type": "text", "text": "not found"}]}
            except ToolFailure as exc:
                result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        self._send(200, {"jsonrpc": "2.0", "id": body["id"], "result": result})

    def _send(self, status, obj):
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except OSError:
            # A client that timed out has already hung up.
            pass


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
