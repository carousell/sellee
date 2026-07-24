"""The attended settings CLI door over the control plane: /control/settings-list and
/control/settings-decide — bearer-gated, harness-independent, working with no channel bound."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

import selly_agent.tools  # noqa: F401  registration
from selly_agent import settings
from selly_agent.config import Config
from selly_agent.http_server import HttpServer
from selly_agent.tools.registry import ToolContext


@pytest.fixture
def server(bus, store, xdg_tmp):
    def context_factory(session):
        return ToolContext(session=session, store=store, bus=bus, config=Config(), started_ts=1.0)

    srv = HttpServer(
        port=0,
        bus=bus,
        store=store,
        events_db_path=bus.store.db.path,
        context_factory=context_factory,
        attended_token="attended-secret",
    )
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def _request(server, method, path, *, token=None, body=None):
    url = f"http://127.0.0.1:{server.port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        return exc.code, (json.loads(raw) if raw else None)


def _propose(store, value=None):
    value = [23, 9] if value is None else value
    cid = store.new_change_id()
    store.propose_setting_change(
        "quiet_hours",
        value,
        change_id=cid,
        prior_value=settings.get(store, "quiet_hours"),
        notice_text="Approve?",
    )
    return cid


# --- list -------------------------------------------------------------------------------------


def test_list_requires_bearer(server) -> None:
    status, _ = _request(server, "GET", "/control/settings-list")
    assert status == 401


def test_list_surfaces_pending_with_id(server, store) -> None:
    cid = _propose(store)
    status, body = _request(server, "GET", "/control/settings-list?token=attended-secret")
    assert status == 200
    assert [p["change_id"] for p in body["pending"]] == [cid]
    assert body["pending"][0]["proposed"] == "23:00–09:00"
    assert any(s["key"] == "quiet_hours" for s in body["settings"])


# --- decide -----------------------------------------------------------------------------------


def test_decide_requires_bearer(server, store) -> None:
    cid = _propose(store)
    status, _ = _request(
        server, "POST", "/control/settings-decide", body={"action": "approve", "change_id": cid}
    )
    assert status == 401


def test_decide_approve_applies(server, store) -> None:
    cid = _propose(store)
    status, body = _request(
        server,
        "POST",
        "/control/settings-decide",
        token="attended-secret",
        body={"action": "approve", "change_id": cid},
    )
    assert status == 200 and body["status"] == "applied"
    assert settings.get(store, "quiet_hours") == [23, 9]
    assert store.get_pending_change(cid)["decided_via"] == "cli"


def test_decide_rejects_bad_action(server, store) -> None:
    cid = _propose(store)
    status, _ = _request(
        server,
        "POST",
        "/control/settings-decide",
        token="attended-secret",
        body={"action": "nuke", "change_id": cid},
    )
    assert status == 400
