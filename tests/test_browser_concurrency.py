"""One Chrome, three actors: the inbox lane, the reply sink, and a browser-driving pass.

The lane and the sink run on different daemon threads and genuinely overlap — a reply can be sent
while the lane is mid-read — and they share one tab. These tests pin the two rules that make that
safe: the mutex serializes whole operations rather than individual calls, and the lane yields
entirely while a pass holds the browser.
"""

from __future__ import annotations

import threading

import pytest

from selly_agent.browser import inbox, sink
from selly_agent.browser.client import BrowserClient
from selly_agent.browser.markets import carousell as carousell_market
from selly_agent.config import Config


class SlowClient:
    """A browser that records every operation as it starts and finishes, so an interleaving shows up
    in the log rather than having to be inferred."""

    def __init__(self):
        self.log: list = []
        self.lock = threading.RLock()
        self.bubbles: list = []
        self.typed = None
        self.url = ""
        self.gate = threading.Event()
        self.gate.set()

    def exclusive(self):
        return _Held(self)

    def _record(self, entry):
        with threading.Lock():
            self.log.append(entry)

    def navigate(self, url):
        self._record(("navigate", url))
        self.url = url
        self.gate.wait(timeout=5)

    def call_tool(self, name, arguments):
        self._record((name, arguments.get("target") or arguments.get("action")))
        if name == "browser_type":
            self.typed = arguments["text"]
        if name == "browser_click" and self.typed is not None:
            self.bubbles.append({"text": self.typed, "side": "out", "y": 9})
        return "ok"

    def evaluate(self, function, **kwargs):
        if function == carousell_market.TAIL_JS:
            self._record(("tail", None))
            return list(self.bubbles)
        if function == carousell_market.LOGIN_JS:
            return {"state": "logged_in"}
        if function == carousell_market.DISCOVERY_JS:
            self._record(("discovery", None))
            return [{"thread_id": "99", "text": "bob 3:18 PM Teak lamp hi", "unread": True}]
        self._record(("probe", None))
        return {"matches": 1, "url": self.url}


class _Held:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        self.client.lock.acquire()
        self.client._record(("enter", None))  # noqa: SLF001 — the stub's own log
        return self.client

    def __exit__(self, *exc):
        self.client._record(("exit", None))  # noqa: SLF001
        self.client.lock.release()
        return False


@pytest.fixture
def seeded(store):
    store.set_seller_config_section("basics", {"region": "SG"})
    item = store.create_item(title="Teak lamp", list_price=80.0, currency="SGD")
    store.create_thread(
        thread_id="carousell:99",
        side="sell",
        market="carousell",
        counterpart_handle="bob",
        item_id=item["id"],
    )
    return item


def test_a_send_arriving_mid_read_serializes_and_both_complete(store, bus, seeded) -> None:
    """Neither operation is atomic on its own — each is several browser calls — so the lock has to
    hold for the whole sequence, not per call."""
    client = SlowClient()
    client.bubbles = [{"text": "hi", "side": "in", "y": 1}]  # a buyer message for the lane to find
    client.gate.clear()  # hold the lane inside its first navigate

    deps = inbox.InboxDeps(
        store=store, bus=bus, config=Config(), browser_factory=lambda: client, now=lambda: 1000.0
    )
    lane = threading.Thread(target=lambda: inbox.inbox_lane(deps))
    lane.start()
    # wait until the lane is genuinely inside the browser, holding the client
    for _ in range(500):
        if ("navigate", "https://www.carousell.sg/inbox/") in client.log:
            break
        threading.Event().wait(0.01)

    from selly_agent.engines import pacing

    reserved = store.reserve_reply(
        thread_id="carousell:99",
        kind="reply",
        text="yes!",
        in_msg_id=None,
        cfg=pacing.resolve(Config(reply_delay_sec=(0, 0)), quiet_hours=(0, 0)),
    )
    reply = sink.BrowserReplySink(client=client, store=store, bus=bus, region="SG")
    sender = threading.Thread(
        target=lambda: reply.send(
            store.get_thread("carousell:99"), "yes!", "reply", reserved["intent_id"]
        )
    )
    sender.start()
    client.gate.set()
    lane.join(timeout=10)
    sender.join(timeout=10)
    assert not lane.is_alive() and not sender.is_alive()

    # the two operations never interleave: every enter is followed by its own exit
    depth = 0
    for entry, _ in client.log:
        if entry == "enter":
            depth += 1
            assert depth == 1
        elif entry == "exit":
            depth -= 1
    assert depth == 0

    # and both did their work
    assert store.get_thread("carousell:99")["message_count"] == 1  # the lane recorded the inbound
    assert client.typed == "yes!"  # the sink sent


def test_the_clients_mutex_is_re_entrant_for_nested_calls(tmp_path) -> None:
    """A compound operation calls tools while already holding the lock, so a non-re-entrant lock
    would deadlock the first send."""
    client = BrowserClient(command=["/nonexistent"])
    with client.exclusive():
        with client.exclusive():
            assert True  # acquiring twice from one thread must not block
