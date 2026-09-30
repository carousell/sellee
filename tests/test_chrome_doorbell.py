"""`chrome.recorded_notifications` against a live CDP endpoint, not a monkeypatch.

What it reads is DevTools' background-services log: armed with `setRecording`, Chrome records every
notification a site shows and replays the lot to whoever starts observing. The shapes below are the
ones a spike recorded from Chrome 154 on 2026-10-01 — the event is `Notification displayed`, the
`instanceId` is the notification's tag, and the metadata carries its title and body and nothing
else.

What these hold: the log is asked for from a session on an ordinary tab and is re-armed on every
look; the page is never scripted; and a browser that cannot be asked answers None, which is never
the same thing as "nothing rang".
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from websockets.sync.server import serve as ws_serve

from sellee.browser import chrome

_FB = "https://www.facebook.com/"


def _event(tag, title, body, *, origin=_FB, when=1790789005.28, name="Notification displayed"):
    return {
        "timestamp": when,
        "origin": origin,
        "serviceWorkerRegistrationId": "2",
        "service": "notifications",
        "eventName": name,
        "instanceId": tag,
        "eventMetadata": [{"key": "Body", "value": body}, {"key": "Title", "value": title}],
        "storageKey": origin,
    }


class _DevtoolsHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        payload = {"/json/version": self.server.version_payload, "/json/list": self.server.targets}
        if self.path not in payload:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(payload[self.path]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


class _FakeCdp:
    def __init__(self, http_server, calls, errors, recorded):
        self.http = http_server
        self.port = http_server.server_address[1]
        self.calls = calls
        self.errors = errors
        self.recorded = recorded


@pytest.fixture
def fake_cdp():
    calls: list = []
    errors: set = set()
    recorded: list = []

    def handler(ws):
        for raw in ws:
            message = json.loads(raw)
            calls.append((message["method"], message.get("sessionId")))
            answer: dict = {"id": message["id"]}
            if message["method"] in errors:
                answer["error"] = {"code": -32601, "message": "method not supported"}
            elif message["method"] == "Target.attachToTarget":
                answer["result"] = {"sessionId": "s-" + message["params"]["targetId"]}
            else:
                answer["result"] = {}
            ws.send(json.dumps(answer))
            if message["method"] == "BackgroundService.startObserving" and "error" not in answer:
                # The replay follows the answer, as Chrome sends it.
                for event in recorded:
                    ws.send(
                        json.dumps(
                            {
                                "method": "BackgroundService.backgroundServiceEventReceived",
                                "params": {"backgroundServiceEvent": event},
                                "sessionId": message.get("sessionId"),
                            }
                        )
                    )

    ws_server = ws_serve(handler, "127.0.0.1", 0)
    ws_port = ws_server.socket.getsockname()[1]
    threading.Thread(target=ws_server.serve_forever, daemon=True).start()

    http_server = ThreadingHTTPServer(("127.0.0.1", 0), _DevtoolsHandler)
    http_server.version_payload = {
        "webSocketDebuggerUrl": f"ws://127.0.0.1:{ws_port}/devtools/browser/fake"
    }
    http_server.targets = [{"id": "t1", "type": "page"}, {"id": "w1", "type": "service_worker"}]
    threading.Thread(target=http_server.serve_forever, args=(0.02,), daemon=True).start()

    try:
        yield _FakeCdp(http_server, calls, errors, recorded)
    finally:
        http_server.shutdown()
        http_server.server_close()
        ws_server.shutdown()


def test_what_rang_is_read_back_with_its_title_body_and_tag(fake_cdp) -> None:
    fake_cdp.recorded.append(_event("mid.$abc", "Gerry Tan", "Gerry: is this still available?"))

    rings = chrome.recorded_notifications(fake_cdp.port)

    assert rings == [
        {
            "origin": _FB,
            "tag": "mid.$abc",
            "shown_ts": 1790789005.28,
            "title": "Gerry Tan",
            "body": "Gerry: is this still available?",
        }
    ]


def test_only_displayed_notifications_are_rings(fake_cdp) -> None:
    """A click or a close is the same notification again, not a new message."""
    fake_cdp.recorded.extend(
        [
            _event("mid.$abc", "Gerry Tan", "hi"),
            _event("mid.$abc", "Gerry Tan", "hi", name="Notification clicked"),
            _event("mid.$abc", "Gerry Tan", "hi", name="Notification closed"),
        ]
    )

    assert [r["tag"] for r in chrome.recorded_notifications(fake_cdp.port)] == ["mid.$abc"]


def test_every_look_re_arms_the_recording_on_a_session_of_an_ordinary_tab(fake_cdp) -> None:
    """DevTools keeps a recording for days, not forever, so each look arms it again. And the log is
    not on the browser target — a page session has it, for every site in the profile."""
    chrome.recorded_notifications(fake_cdp.port)

    methods = [m for m, _ in fake_cdp.calls]
    assert methods.index("BackgroundService.setRecording") < methods.index(
        "BackgroundService.startObserving"
    )
    sessions = {s for m, s in fake_cdp.calls if m.startswith("BackgroundService.")}
    assert sessions == {"s-t1"}


def test_the_page_is_never_scripted(fake_cdp) -> None:
    """Listening must cost the marketplace's page nothing it could notice."""
    chrome.recorded_notifications(fake_cdp.port)

    methods = {m for m, _ in fake_cdp.calls}
    assert not {m for m in methods if m.startswith(("Runtime.", "Page.", "Emulation.", "DOM."))}


def test_the_session_is_let_go_afterwards(fake_cdp) -> None:
    chrome.recorded_notifications(fake_cdp.port)

    methods = [m for m, _ in fake_cdp.calls]
    assert "BackgroundService.stopObserving" in methods
    assert methods[-1] == "Target.detachFromTarget"


def test_a_chrome_with_no_tab_open_cannot_be_asked(fake_cdp) -> None:
    fake_cdp.http.targets = [{"id": "w1", "type": "service_worker"}]

    assert chrome.recorded_notifications(fake_cdp.port) is None


def test_a_chrome_that_will_not_record_cannot_be_asked(fake_cdp) -> None:
    fake_cdp.errors.add("BackgroundService.setRecording")

    assert chrome.recorded_notifications(fake_cdp.port) is None


def test_a_port_with_nothing_on_it_cannot_be_asked() -> None:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        free = sock.getsockname()[1]

    assert chrome.recorded_notifications(free) is None


def test_nothing_rang_is_an_empty_list_not_none(fake_cdp) -> None:
    assert chrome.recorded_notifications(fake_cdp.port) == []
