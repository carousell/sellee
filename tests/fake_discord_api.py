"""An in-process fake Discord REST API for the channel tests — mirrors tests/fake_telegram_api.py:
a stdlib ThreadingHTTPServer serving the endpoints our transport calls, with a scriptable call log
and outbox, so tests drive the REAL transport against it rather than a mock.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOT_ID = "123456789012345678"
APPLICATION_ID = "123456789012345678"
BOT = {"id": BOT_ID, "username": "sellee_test_bot", "bot": True}
CHANNEL_ID = 987654321098765432
# A structurally fake Discord bot token (three dot-separated base64url-shaped segments), never a
# real credential. Assembled from separate parts, none of which is itself a single
# three-dot-segment literal, so neither the leak guard nor a pattern-based secret scanner
# (GitHub's push protection included) ever sees the whole shape sitting in one string in the
# source — the assembly is what keeps it clear of those patterns, not the segment lengths.
_FAKE_SNOWFLAKE_SEGMENT = "OTIzNDU2Nzg5MDEyMzQ1Njc4"
_FAKE_TIMESTAMP_SEGMENT = "YIz98g"
_FAKE_HMAC_SEGMENT = "NOTAREALSECRET" + "x" * 40
FAKE_TOKEN = ".".join([_FAKE_SNOWFLAKE_SEGMENT, _FAKE_TIMESTAMP_SEGMENT, _FAKE_HMAC_SEGMENT])


class FakeDiscordAPI:
    def __init__(self):
        self.calls: list = []
        self.outbox: list = []
        self.typing_pulses: list = []
        self.acknowledged: list = []
        self.callbacks: list = []
        self.user_agents: list = []
        self.files: dict = {"/attachments/fake.jpg": b"\xff\xd8\xff\xe0fake-jpeg-bytes"}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._server.daemon_threads = True
        self._server.timeout = 0.5
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._thread.join()
        # shutdown() only stops the accept loop; the listening socket stays open without this.
        self._server.server_close()

    def _make_handler(self):
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _reply(self, status: int, body: dict) -> None:
                payload = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(length)) if length else {}

            def _rejects_blocked_user_agent(self) -> bool:
                # Discord's edge 403s urllib's default User-Agent before reading the token, on
                # every route including the CDN. Reproducing it makes the whole suite a net for a
                # missing header; without it, a transport that sends none passes every test.
                if not (self.headers.get("User-Agent") or "").startswith("DiscordBot "):
                    self.send_response(403)
                    self.send_header("Content-Length", "17")
                    self.end_headers()
                    self.wfile.write(b"error code: 1010\n")
                    return True
                return False

            def _rejects_bad_bot_token(self) -> bool:
                # Attachment CDN links are unauthenticated by design (see transport.py's
                # download_attachment docstring); every /api/v10/* route requires the real fake
                # bot token, matching Discord's own 401-on-bad-token behavior so a transport bug
                # that mishandles auth failure is exercised for real, not mocked away.
                if (
                    self.path.startswith("/api/v10/")
                    and self.headers.get("Authorization") != f"Bot {FAKE_TOKEN}"
                ):
                    self._reply(401, {"message": "401: Unauthorized", "code": 0})
                    return True
                return False

            def do_GET(self) -> None:
                api.calls.append(("GET", self.path))
                api.user_agents.append(self.headers.get("User-Agent"))
                if self._rejects_blocked_user_agent() or self._rejects_bad_bot_token():
                    return
                if self.path.startswith("/attachments/"):
                    data = api.files.get(self.path, api.files["/attachments/fake.jpg"])
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/api/v10/users/@me":
                    self._reply(200, BOT)
                elif self.path == "/api/v10/oauth2/applications/@me":
                    self._reply(200, {"id": APPLICATION_ID})
                else:
                    self._reply(404, {"message": "unknown"})

            def do_POST(self) -> None:
                api.calls.append(("POST", self.path))
                api.user_agents.append(self.headers.get("User-Agent"))
                if self._rejects_blocked_user_agent() or self._rejects_bad_bot_token():
                    return
                body = self._body()
                if self.path == f"/api/v10/channels/{CHANNEL_ID}/messages":
                    api.outbox.append(body)
                    self._reply(200, {"id": "111", "content": body.get("content", "")})
                elif self.path == f"/api/v10/channels/{CHANNEL_ID}/typing":
                    api.typing_pulses.append(True)
                    self._reply(204, {})
                elif self.path.startswith("/api/v10/interactions/") and self.path.endswith(
                    "/callback"
                ):
                    api.acknowledged.append(self.path)
                    # The body too: type 6 acks the click, type 7 acks it and strips the buttons.
                    api.callbacks.append(body)
                    self._reply(204, {})
                else:
                    self._reply(404, {"message": "unknown"})

        return Handler
