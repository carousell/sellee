"""`selly-agent connect telegram` client UX: interactive getpass + BotFather guidance, the piped
token read, the printed phone-delivery URL, the interactive/piped timeout defaults, and token
hygiene. The daemon routes are exercised in test_channel_bind; here the HTTP calls are stubbed so
the CLI's own behaviour is isolated.
"""

from __future__ import annotations

import io
import urllib.error

import pytest

from fake_telegram_api import FAKE_TOKEN as _TOKEN
from selly_agent import connect_cli


@pytest.fixture
def stub_daemon(monkeypatch):
    """Stub the two HTTP calls. Records the POST body; channel-status replies per `bound`."""
    calls = {"posts": []}

    def fake_post(url, token, body):
        calls["posts"].append((url, token, body))
        return calls.get("post_status", 200), calls.get(
            "post_body", {"bot_username": "sellybot", "start_url": "https://t.me/sellybot?start=n0"}
        )

    def fake_get(url):
        return {"bound": calls.get("bound", True), "bot_username": "sellybot"}

    monkeypatch.setattr(connect_cli, "_post", fake_post)
    monkeypatch.setattr(connect_cli, "_get", fake_get)
    return calls


def _pipe_stdin(monkeypatch, text):
    monkeypatch.setattr(connect_cli.sys, "stdin", io.StringIO(text))


# --- token entry: piped path is unchanged ----------------------------------------------------


def test_piped_empty_token_exits_2(monkeypatch, capsys) -> None:
    _pipe_stdin(monkeypatch, "")
    called = {"getpass": False}
    monkeypatch.setattr(
        connect_cli.getpass,
        "getpass",
        lambda *a, **k: called.__setitem__("getpass", True) or "",
    )
    rc = connect_cli.bind_flow(9999, "mcp-tok", interactive=False)
    assert rc == 2
    assert not called["getpass"]  # piped path never prompts
    assert "no token on stdin — pipe the BotFather token in" in capsys.readouterr().err


def test_piped_reads_token_via_readline_and_binds(monkeypatch, stub_daemon, capsys) -> None:
    _pipe_stdin(monkeypatch, _TOKEN + "\n")
    monkeypatch.setattr(
        connect_cli.getpass, "getpass", lambda *a, **k: pytest.fail("getpass used on piped path")
    )
    rc = connect_cli.bind_flow(9999, "mcp-tok", interactive=False)
    assert rc == 0
    assert stub_daemon["posts"][0][2] == {"token": _TOKEN}  # exact token forwarded
    out = capsys.readouterr().out
    assert "Connected as @sellybot." in out
    assert "BotFather" not in out  # no interactive guidance on the piped path


# --- token entry: interactive path -----------------------------------------------------------


def test_interactive_prints_guidance_and_uses_getpass(monkeypatch, stub_daemon, capsys) -> None:
    _pipe_stdin(monkeypatch, "should-not-be-read\n")  # readline must be bypassed
    monkeypatch.setattr(connect_cli.getpass, "getpass", lambda *a, **k: _TOKEN)
    rc = connect_cli.bind_flow(9999, "mcp-tok", interactive=True)
    assert rc == 0
    assert stub_daemon["posts"][0][2] == {"token": _TOKEN}  # getpass token, not the piped bytes
    out = capsys.readouterr().out
    assert "message @BotFather" in out and "/newbot" in out  # BotFather guidance above the prompt


def test_interactive_empty_token_exits_2(monkeypatch, capsys) -> None:
    monkeypatch.setattr(connect_cli.getpass, "getpass", lambda *a, **k: "")
    rc = connect_cli.bind_flow(9999, "mcp-tok", interactive=True)
    assert rc == 2
    assert "no token entered" in capsys.readouterr().err


def test_interactive_getpass_interrupt_exits_2(monkeypatch, capsys) -> None:
    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(connect_cli.getpass, "getpass", boom)
    rc = connect_cli.bind_flow(9999, "mcp-tok", interactive=True)
    assert rc == 2


def test_autodetects_interactive_from_isatty(monkeypatch, stub_daemon, capsys) -> None:
    monkeypatch.setattr(connect_cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(connect_cli.getpass, "getpass", lambda *a, **k: _TOKEN)
    rc = connect_cli.bind_flow(9999, "mcp-tok")  # interactive not passed → detect
    assert rc == 0
    assert "BotFather" in capsys.readouterr().out


# --- error mapping ---------------------------------------------------------------------------


def test_bad_token_format_exits_2(monkeypatch, stub_daemon, capsys) -> None:
    _pipe_stdin(monkeypatch, _TOKEN + "\n")
    stub_daemon["post_status"] = 400
    stub_daemon["post_body"] = {"error": "bad_token_format"}
    rc = connect_cli.bind_flow(9999, "mcp-tok", interactive=False)
    assert rc == 2
    assert "token rejected (bad_token_format)" in capsys.readouterr().err


def test_daemon_unreachable_exits_3(monkeypatch, capsys) -> None:
    _pipe_stdin(monkeypatch, _TOKEN + "\n")

    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(connect_cli, "_post", boom)
    rc = connect_cli.bind_flow(9999, "mcp-tok", interactive=False)
    assert rc == 3
    assert "could not reach the daemon" in capsys.readouterr().err


def test_timeout_exits_1(monkeypatch, stub_daemon, capsys) -> None:
    _pipe_stdin(monkeypatch, _TOKEN + "\n")
    stub_daemon["bound"] = False  # never binds
    rc = connect_cli.bind_flow(9999, "mcp-tok", interactive=False, timeout=0)
    assert rc == 1
    assert "Timed out waiting for /start" in capsys.readouterr().err


# --- phone-delivery affordance ---------------------------------------------------------------


def test_prints_prominent_url_with_phone_wording(monkeypatch, stub_daemon, capsys) -> None:
    _pipe_stdin(monkeypatch, _TOKEN + "\n")
    rc = connect_cli.bind_flow(9999, "mcp-tok", interactive=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Open this link on the phone that has Telegram" in out
    assert "https://t.me/sellybot?start=n0" in out


# --- timeout defaults ------------------------------------------------------------------------


def test_interactive_default_timeout_is_300(monkeypatch, stub_daemon, capsys) -> None:
    monkeypatch.setattr(connect_cli.getpass, "getpass", lambda *a, **k: _TOKEN)
    connect_cli.bind_flow(9999, "mcp-tok", interactive=True)
    assert "up to 300s" in capsys.readouterr().out


def test_piped_default_timeout_is_120(monkeypatch, stub_daemon, capsys) -> None:
    _pipe_stdin(monkeypatch, _TOKEN + "\n")
    connect_cli.bind_flow(9999, "mcp-tok", interactive=False)
    assert "up to 120s" in capsys.readouterr().out


# --- token hygiene ---------------------------------------------------------------------------


def test_token_never_printed_to_stdout(monkeypatch, stub_daemon, capsys) -> None:
    monkeypatch.setattr(connect_cli.getpass, "getpass", lambda *a, **k: _TOKEN)
    connect_cli.bind_flow(9999, "mcp-tok", interactive=True)
    captured = capsys.readouterr()
    assert _TOKEN not in captured.out and _TOKEN not in captured.err
