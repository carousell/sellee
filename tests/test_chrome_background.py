"""enable_background_operation against a live CDP endpoint, not a monkeypatch.

Everything else that touches this function stubs it out, which is how two real failure modes
went unpinned: a /json/version answer that is valid JSON but not an object (a squatter on the
port, or a Chrome asked mid-start) and a Chrome that answers an emulation call with an error
rather than a result (a version that does not speak the method). The first must be a quiet zero,
never a crash that takes the read lane with it; the second must not count the tab as prepared —
an unprepared tab falls back to the raise, and a tab wrongly counted prepared just goes unread.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from websockets.sync.server import serve as ws_serve

from sellee.browser import chrome


class _DevtoolsHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/json/version":
            payload = self.server.version_payload
        elif self.path == "/json/list":
            payload = self.server.targets
        else:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


class _FakeCdp:
    """An HTTP devtools front and the browser websocket behind it, both configurable per test."""

    def __init__(self, http_server, port, calls, errors):
        self.http = http_server
        self.port = port
        self.calls = calls
        # Methods the websocket answers with an error instead of a result. Mutate in a test.
        self.errors = errors


@pytest.fixture
def fake_cdp():
    calls: list = []
    errors: set = set()

    def handler(ws):
        for raw in ws:
            message = json.loads(raw)
            calls.append(message["method"])
            answer: dict = {"id": message["id"]}
            if message["method"] in errors:
                answer["error"] = {"code": -32601, "message": "method not supported"}
            elif message["method"] == "Target.attachToTarget":
                answer["result"] = {"sessionId": "s-" + message["params"]["targetId"]}
            else:
                answer["result"] = {}
            ws.send(json.dumps(answer))

    ws_server = ws_serve(handler, "127.0.0.1", 0)
    ws_port = ws_server.socket.getsockname()[1]
    threading.Thread(target=ws_server.serve_forever, daemon=True).start()

    http_server = ThreadingHTTPServer(("127.0.0.1", 0), _DevtoolsHandler)
    http_server.version_payload = {
        "webSocketDebuggerUrl": f"ws://127.0.0.1:{ws_port}/devtools/browser/fake"
    }
    http_server.targets = [
        {"id": "t1", "type": "page"},
        {"id": "t2", "type": "page"},
        {"id": "w1", "type": "service_worker"},
    ]
    threading.Thread(target=http_server.serve_forever, args=(0.02,), daemon=True).start()

    try:
        yield _FakeCdp(http_server, http_server.server_address[1], calls, errors)
    finally:
        http_server.shutdown()
        http_server.server_close()
        ws_server.shutdown()


def test_every_page_target_is_prepared_and_nothing_else(fake_cdp) -> None:
    assert chrome.enable_background_operation(fake_cdp.port) == 2
    assert fake_cdp.calls.count("Target.attachToTarget") == 2
    assert fake_cdp.calls.count("Emulation.setFocusEmulationEnabled") == 2
    assert fake_cdp.calls.count("Page.setWebLifecycleState") == 2


def test_a_version_answer_that_is_not_an_object_is_a_zero_not_a_crash(fake_cdp) -> None:
    """Anything can be squatting on the port, and Chrome mid-start answers oddly too. The daemon
    calls this on every acquisition, so a crash here is the read lane going dark in scheduler
    backoff — the one caller that must never see an exception."""
    fake_cdp.http.version_payload = []
    assert chrome.enable_background_operation(fake_cdp.port) == 0


def test_a_chrome_that_rejects_the_emulation_is_not_counted_prepared(fake_cdp) -> None:
    """An old Chrome answers the method with an error, not a result. Counting that tab prepared
    would report background reading works on a browser where it cannot."""
    fake_cdp.errors.add("Emulation.setFocusEmulationEnabled")
    assert chrome.enable_background_operation(fake_cdp.port) == 0
