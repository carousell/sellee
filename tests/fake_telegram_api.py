"""An in-process fake Telegram Bot API server for the channel tests.

A stdlib ThreadingHTTPServer serving the methods our transport calls (getMe, getUpdates,
sendMessage, sendChatAction, answerCallbackQuery, setMyCommands, getFile + file download). It
holds a scriptable update queue and a call log, so a test drives the REAL transport against it —
bind, sends, polls — without touching Telegram.

Usage:

    with FakeTelegramAPI() as api:
        api.inject_command("/start nonce-abc")
        client = TelegramClient(FAKE_TOKEN, api_base=api.base_url)
        ...

`calls` records every method name in order (the nonce matrix asserts an off poller records none).
`outbox` records every sendMessage. Injection helpers append updates the seller "sent".
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# A fake seller chat + bot. The digits are on the leak-guard's approved-fake allowlist.
CHAT_ID = 123456789
BOT = {"id": 123456789, "is_bot": True, "first_name": "SelleeTest", "username": "sellee_test_bot"}
# A well-formed but obviously-fake bot token for tests. Its auth part deliberately does NOT start
# with the "AA" prefix the leak guard keys on, so a real-credential-shape match never trips on it.
FAKE_TOKEN = "123456789:ZZfakeBotTokenForTestsOnlyNeverRealXX"


class FakeTelegramAPI:
    def __init__(self, *, longpoll_cap_sec: float = 0.3):
        self._lock = threading.Lock()
        self._next_update_id = 1
        self._queue: list = []
        self.offset = 0
        self.outbox: list = []
        self.calls: list = []
        self.commands: list | None = None
        self.chat_actions: list = []
        self.answered: list = []
        self.files: dict = {"_default": b"\xff\xd8\xff\xe0fake-jpeg-bytes"}
        self._longpoll_cap = longpoll_cap_sec
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._server.daemon_threads = True
        # shutdown() cannot return until the accept loop next wakes, and the stdlib default of
        # 0.5s is a wait every single test using this double would pay on exit.
        self._thread = threading.Thread(
            target=self._server.serve_forever, args=(0.02,), daemon=True
        )

    # --- lifecycle ---
    def __enter__(self) -> FakeTelegramAPI:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://127.0.0.1:{port}"

    # --- injection (the seller side) ---
    def _append(self, update_body: dict) -> int:
        with self._lock:
            uid = self._next_update_id
            self._next_update_id += 1
            update_body["update_id"] = uid
            self._queue.append(update_body)
            return uid

    def inject_text(self, text: str, *, chat_id: int = CHAT_ID) -> int:
        chat = {"id": chat_id, "type": "private", "first_name": "Seller"}
        return self._append(
            {"message": {"message_id": 1000, "date": 1, "chat": chat, "text": text}}
        )

    def inject_command(self, text: str, *, chat_id: int = CHAT_ID) -> int:
        return self.inject_text(text, chat_id=chat_id)

    def inject_photo(self, caption: str = "", *, file_id: str = "photo1", chat_id: int = CHAT_ID):
        chat = {"id": chat_id, "type": "private", "first_name": "Seller"}
        return self._append(
            {
                "message": {
                    "message_id": 1001,
                    "date": 2,
                    "chat": chat,
                    "caption": caption,
                    "photo": [
                        {"file_id": file_id, "file_size": 60000, "width": 1024, "height": 768}
                    ],
                }
            }
        )

    def inject_tap(self, callback_data: str, *, chat_id: int = CHAT_ID) -> int:
        chat = {"id": chat_id, "type": "private"}
        return self._append(
            {
                "callback_query": {
                    "id": "cbq1",
                    "data": callback_data,
                    "message": {"message_id": 1002, "date": 3, "chat": chat},
                }
            }
        )

    # --- the handler ---
    def _make_handler(self):
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a) -> None:  # quiet
                pass

            def _reply(self, obj: dict, code: int = 200) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _params(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode() if length else ""
                try:
                    return json.loads(raw) if raw else {}
                except ValueError:
                    return {}

            def do_POST(self) -> None:  # noqa: N802
                self._route()

            def do_GET(self) -> None:  # noqa: N802
                self._route()

            def _route(self) -> None:
                parts = self.path.split("?")[0].strip("/").split("/")
                if len(parts) >= 2 and parts[0] == "file":
                    return self._serve_file(parts)
                method = parts[-1] if parts else ""
                with api._lock:
                    api.calls.append(method)
                p = self._params()
                if method == "getMe":
                    return self._reply({"ok": True, "result": BOT})
                if method == "setMyCommands":
                    with api._lock:
                        api.commands = p.get("commands")
                    return self._reply({"ok": True, "result": True})
                if method == "sendChatAction":
                    with api._lock:
                        api.chat_actions.append(p.get("chat_id"))
                    return self._reply({"ok": True, "result": True})
                if method == "answerCallbackQuery":
                    with api._lock:
                        api.answered.append(p.get("callback_query_id"))
                    return self._reply({"ok": True, "result": True})
                if method == "sendMessage":
                    with api._lock:
                        mid = len(api.outbox) + 1
                        api.outbox.append(
                            {
                                "message_id": mid,
                                "chat_id": p.get("chat_id"),
                                "text": p.get("text", ""),
                                "reply_markup": p.get("reply_markup"),
                            }
                        )
                    return self._reply(
                        {"ok": True, "result": {"message_id": mid, "text": p.get("text", "")}}
                    )
                if method == "getUpdates":
                    return self._get_updates(p)
                if method == "getFile":
                    fid = p.get("file_id") or ""
                    return self._reply(
                        {"ok": True, "result": {"file_id": fid, "file_path": f"photos/{fid}.jpg"}}
                    )
                return self._reply({"ok": False, "description": f"method {method} not faked"}, 404)

            def _get_updates(self, p: dict) -> None:
                offset = int(p.get("offset") or 0)
                with api._lock:
                    api.offset = max(api.offset, offset)
                    pending = [u for u in api._queue if u["update_id"] >= offset]
                deadline = time.time() + min(int(p.get("timeout") or 0), api._longpoll_cap)
                while not pending and time.time() < deadline:
                    time.sleep(0.02)
                    with api._lock:
                        pending = [u for u in api._queue if u["update_id"] >= offset]
                return self._reply({"ok": True, "result": pending})

            def _serve_file(self, parts: list) -> None:
                # /file/bot<token>/photos/<file_id>.jpg → the file_id-keyed bytes (or the default).
                name = parts[-1].rsplit(".", 1)[0]
                with api._lock:
                    body = api.files.get(name, api.files["_default"])
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler
