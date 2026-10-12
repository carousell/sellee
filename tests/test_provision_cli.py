"""`sellee provision carousell-ai`: the key is stored by the rail, and the currency registration
answered is recorded through the daemon, as setup does. The rail and daemon calls are stubbed."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from sellee import config as config_mod
from sellee import control, pass_cli
from sellee.rail import provision as rail_provision

_OK = {"status": "ok", "provisioned": True, "country": "VN", "notice": "", "currency": "VND"}


@pytest.fixture
def world(monkeypatch):
    calls = {"posts": [], "status": dict(_OK), "post_status": 200}

    def fake_post(port, token, route, body, **kwargs):
        calls["posts"].append((route, body))
        return calls["post_status"], {"error": "nope"}

    monkeypatch.setattr(rail_provision, "ensure", lambda region, **kwargs: calls["status"])
    monkeypatch.setattr(control, "post", fake_post)
    monkeypatch.setattr(control, "require_token", lambda: "tok")
    monkeypatch.setattr(config_mod, "load", lambda path=None: config_mod.Config())
    return calls


def _provision() -> int:
    return pass_cli.provision(SimpleNamespace(region="VN"))


def test_the_currency_registration_answered_is_recorded(world, capsys) -> None:
    assert _provision() == 0

    assert world["posts"] == [("/control/seller-basics", {"currency": "VND"})]
    assert json.loads(capsys.readouterr().out)["currency"] == "VND"


def test_no_currency_answered_records_nothing(world) -> None:
    del world["status"]["currency"]

    assert _provision() == 0

    assert world["posts"] == []


def test_a_failed_provision_records_nothing(world) -> None:
    world["status"] = {"status": "error", "error": "offline"}

    assert _provision() == 3

    assert world["posts"] == []


def test_a_refused_record_warns_but_the_key_still_counts(world, capsys) -> None:
    world["post_status"] = 400

    assert _provision() == 0

    assert "could not record your currency (VND): nope" in capsys.readouterr().err


def test_an_unreachable_daemon_warns_but_the_key_still_counts(world, monkeypatch, capsys) -> None:
    def down(*args, **kwargs):
        raise control.DaemonUnreachable("connection refused")

    monkeypatch.setattr(control, "post", down)

    assert _provision() == 0

    assert "could not record your currency (VND)" in capsys.readouterr().err
