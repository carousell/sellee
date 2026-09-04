"""The buyer simulator: the two ends of the reply loop it stands in for, and its gate."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from sellee import buyer_sim
from sellee.browser.client import BrowserError
from sellee.config import Config
from sellee.http_server import HttpServer
from sellee.store import ScopedStore
from sellee.tools.registry import ToolContext

_ATTENDED = "attended-token-for-tests"


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("SELLEE_BUYER_SIM", "1")


@pytest.fixture
def disabled(monkeypatch):
    monkeypatch.delenv("SELLEE_BUYER_SIM", raising=False)


def test_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("SELLEE_BUYER_SIM", raising=False)
    assert buyer_sim.enabled() is False
    for off in ("", "0", "false", "no"):
        monkeypatch.setenv("SELLEE_BUYER_SIM", off)
        assert buyer_sim.enabled() is False, off
    monkeypatch.setenv("SELLEE_BUYER_SIM", "1")
    assert buyer_sim.enabled() is True


def test_simulated_threads_are_on_their_own_market():
    """Simulated conversations must be distinguishable by thread id alone: that is what keeps the
    browser inbox from ever trying to read one with Chrome."""
    assert buyer_sim.thread_id_for("7") == "simbuyer:7"
    assert buyer_sim.is_sim_thread("simbuyer:7")
    assert not buyer_sim.is_sim_thread("carousell:7")
    assert not buyer_sim.is_sim_thread("")


def test_the_sim_market_is_not_a_browser_market():
    """If it ever became one, the inbox lane would drive Chrome at a conversation that does not
    exist. Assert the separation rather than trusting it."""
    from sellee import marketplaces

    assert buyer_sim.SIM_MARKET not in set(marketplaces.browser_markets())


def test_sink_delivers_nothing_and_returns_for_a_simulated_thread():
    """Returning cleanly is the whole job: send_reply only reaches commit_reply when the sink
    returns, and that commit is what makes the agent's words readable."""
    sink = buyer_sim.SimReplySink()
    assert sink.send({"thread_id": "simbuyer:1"}, "hello", "reply", "int_1") is None


def test_sink_refuses_a_real_thread_rather_than_swallowing_it():
    """Silently accepting would leave a real buyer unanswered with no trace. Raising leaves the
    intent pending, which the stale sweep turns into an escalation the seller can see."""
    sink = buyer_sim.SimReplySink()
    with pytest.raises(BrowserError) as excinfo:
        sink.send({"thread_id": "carousell:9"}, "hello", "reply", "int_1")
    assert "carousell:9" in str(excinfo.value)


def test_recording_a_buyer_message_creates_the_thread_once(store):
    item = store.create_item(title="Lamp", list_price=100.0, currency="SGD")

    first = buyer_sim.record_buyer_message(
        store, item_id=item["id"], handle="bob", text="is this available?", local_id="1"
    )
    second = buyer_sim.record_buyer_message(
        store, item_id=item["id"], handle="bob", text="would you take 70?", local_id="1"
    )

    assert first["thread_id"] == second["thread_id"] == "simbuyer:1"
    texts = [m["text"] for m in store.get_thread_messages("simbuyer:1")]
    assert texts == ["is this available?", "would you take 70?"]


def test_recording_does_not_advance_the_cursor(store):
    """Only a committed reply may advance it — otherwise the message the buyer just sent would be
    treated as already handled and the agent would never answer."""
    item = store.create_item(title="Lamp", list_price=100.0, currency="SGD")
    buyer_sim.record_buyer_message(
        store, item_id=item["id"], handle="bob", text="hello?", local_id="1"
    )
    thread = store.get_thread("simbuyer:1")
    assert not thread.get("cursor_last_msg_id")


# --- the routes -------------------------------------------------------------------------------


def _server(bus, store):
    def context_factory(session):
        return ToolContext(
            session=session,
            store=ScopedStore(store, getattr(session, "scope", None)),
            bus=bus,
            config=Config(),
            reply_sink=lambda: buyer_sim.SimReplySink(bus=bus),
            started_ts=1.0,
        )

    server = HttpServer(
        port=0,
        bus=bus,
        store=store,
        events_db_path=bus.store.db.path,
        context_factory=context_factory,
        attended_token=_ATTENDED,
    )
    server.start()
    return server


def _post(server, route, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.port}{route}",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_ATTENDED}",
            "Origin": "http://127.0.0.1",
        },
    )
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _get(server, route):
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.port}{route}", headers={"Origin": "http://127.0.0.1"}
    )
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_routes_are_absent_unless_the_simulator_is_enabled(bus, store, disabled, xdg_tmp):
    """A daemon that was never told to simulate has no simulator — not a disabled one."""
    server = _server(bus, store)
    try:
        assert _post(server, "/control/sim-inbound", {"item_id": "x", "text": "hi"})[0] == 404
        assert _get(server, "/buyer")[0] == 404
        assert _get(server, f"/control/sim-items?token={_ATTENDED}")[0] == 404
    finally:
        server.stop()


def test_inbound_route_records_the_message_and_queues_a_reply(bus, store, enabled, xdg_tmp):
    """Queued directly rather than left to the reply lane: the lane's cooldown and pacing gate
    exist to look human on a real marketplace and only get in the way of a rehearsal."""
    item = store.create_item(title="Lamp", list_price=100.0, currency="SGD")
    server = _server(bus, store)
    try:
        status, body = _post(
            server,
            "/control/sim-inbound",
            {"item_id": item["id"], "text": "would you take 70?", "handle": "bob"},
        )
    finally:
        server.stop()

    assert status == 200
    assert body["thread_id"] == "simbuyer:1"
    assert body["pass_id"]
    assert [m["text"] for m in store.get_thread_messages("simbuyer:1")] == ["would you take 70?"]
    queued = store.claim_queued_pass()
    assert queued.type == "reply"
    payload = queued.payload
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["thread_ids"] == ["simbuyer:1"]


def test_the_page_is_served_when_enabled(bus, store, enabled, xdg_tmp):
    """Covers the packaged asset too: the page is read from disk per request, so a missing or
    unpackaged buyer.html fails here rather than in a browser."""
    server = _server(bus, store)
    try:
        status, body = _get(server, "/buyer")
    finally:
        server.stop()
    assert status == 200
    assert b"buyer simulator" in body
    # The page must never be handed a token in its own URL; it trades a one-shot ticket instead.
    assert b"tail-exchange" in body


def test_inbound_route_requires_an_item_and_text(bus, store, enabled, xdg_tmp):
    server = _server(bus, store)
    try:
        assert _post(server, "/control/sim-inbound", {"text": "hi"})[0] == 400
        assert _post(server, "/control/sim-inbound", {"item_id": "x"})[0] == 400
    finally:
        server.stop()


def test_thread_route_refuses_a_real_thread(bus, store, enabled, xdg_tmp):
    """The read side is for rehearsals; a real buyer's transcript is not what this page is for."""
    server = _server(bus, store)
    try:
        code, _ = _get(server, f"/control/sim-thread?token={_ATTENDED}&thread_id=carousell:1")
    finally:
        server.stop()
    assert code == 400
