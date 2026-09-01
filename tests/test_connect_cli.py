"""`sellee connect telegram` client UX: interactive getpass + BotFather guidance, the piped
token read, the printed phone-delivery URL, the interactive/piped timeout defaults, and token
hygiene. The daemon routes are exercised in test_channel_bind; here the HTTP calls are stubbed so
the CLI's own behaviour is isolated.
"""

from __future__ import annotations

import io

import pytest

from fake_discord_api import FAKE_TOKEN as _DISCORD_TOKEN
from fake_telegram_api import FAKE_TOKEN as _TOKEN
from sellee import connect_cli, control
from sellee.config import Config


@pytest.fixture
def stub_daemon(monkeypatch):
    """Stub the two HTTP calls. Records the POST body; channel-status replies per `bound`."""
    calls = {"posts": []}

    def fake_post(port, token, route, body, **kwargs):
        calls["posts"].append((route, token, body))
        return calls.get("post_status", 200), calls.get(
            "post_body",
            {"bot_username": "selleebot", "start_url": "https://t.me/selleebot?start=n0"},
        )

    def fake_get(port, token, route, params=None, **kwargs):
        return 200, {
            "adapter": "telegram",
            "bound": calls.get("bound", True),
            "bot_username": "selleebot",
        }

    monkeypatch.setattr(control, "post", fake_post)
    monkeypatch.setattr(control, "get", fake_get)
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
    assert "Connected as @selleebot." in out
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
        raise control.DaemonUnreachable("connection refused")

    monkeypatch.setattr(control, "post", boom)
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
    assert "Scan the code with the phone that has Telegram" in out
    assert "https://t.me/selleebot?start=n0" in out


def test_prints_a_terminal_qr_above_the_link(monkeypatch, stub_daemon, capsys) -> None:
    _pipe_stdin(monkeypatch, _TOKEN + "\n")
    connect_cli.bind_flow(9999, "mcp-tok", interactive=False)
    out = capsys.readouterr().out
    qr_block = out.split("validated.", 1)[1].split("Scan the code", 1)[0]
    assert "█" in qr_block  # the half-block QR sits between the identity and the guidance
    assert "\x1b" not in out  # the render is colorless — nothing ANSI lands in a pipe or a log


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


# --- Discord: discord_bind_flow, mirroring bind_flow's coverage above -------------------------


@pytest.fixture
def stub_discord_daemon(monkeypatch):
    """Discord's analog of `stub_daemon` — same shape, a different default POST body (invite_url
    + nonce instead of a Telegram start_url)."""
    calls = {"posts": []}

    def fake_post(port, token, route, body, **kwargs):
        calls["posts"].append((route, token, body))
        return calls.get("post_status", 200), calls.get(
            "post_body",
            {
                "bot_username": "selleebot",
                "application_id": "999",
                "invite_url": "https://discord.com/oauth2/authorize?client_id=999&scope=bot&permissions=0",
                "nonce": "n0",
            },
        )

    def fake_get(port, token, route, params=None, **kwargs):
        return 200, {
            "adapter": "discord",
            "bound": calls.get("bound", True),
            "bot_username": "selleebot",
        }

    monkeypatch.setattr(control, "post", fake_post)
    monkeypatch.setattr(control, "get", fake_get)
    return calls


def test_discord_piped_empty_token_exits_2(monkeypatch, capsys) -> None:
    _pipe_stdin(monkeypatch, "")
    called = {"getpass": False}
    monkeypatch.setattr(
        connect_cli.getpass,
        "getpass",
        lambda *a, **k: called.__setitem__("getpass", True) or "",
    )
    rc = connect_cli.discord_bind_flow(9999, "mcp-tok", interactive=False)
    assert rc == 2
    assert not called["getpass"]
    assert "no token on stdin — pipe the Developer Portal token in" in capsys.readouterr().err


def test_discord_piped_reads_token_via_readline_and_binds(
    monkeypatch, stub_discord_daemon, capsys
) -> None:
    _pipe_stdin(monkeypatch, _DISCORD_TOKEN + "\n")
    monkeypatch.setattr(
        connect_cli.getpass, "getpass", lambda *a, **k: pytest.fail("getpass used on piped path")
    )
    rc = connect_cli.discord_bind_flow(9999, "mcp-tok", interactive=False)
    assert rc == 0
    assert stub_discord_daemon["posts"][0][2] == {"token": _DISCORD_TOKEN}
    out = capsys.readouterr().out
    assert "Connected as @selleebot." in out
    assert "Developer Portal" not in out  # no interactive guidance on the piped path


def test_discord_interactive_prints_guidance_and_prompts_for_a_discord_token(
    monkeypatch, stub_discord_daemon, capsys
) -> None:
    _pipe_stdin(monkeypatch, "should-not-be-read\n")
    prompts = []
    monkeypatch.setattr(
        connect_cli.getpass,
        "getpass",
        lambda prompt="", *a, **k: prompts.append(prompt) or _DISCORD_TOKEN,
    )
    rc = connect_cli.discord_bind_flow(9999, "mcp-tok", interactive=True)
    assert rc == 0
    assert stub_discord_daemon["posts"][0][2] == {"token": _DISCORD_TOKEN}
    out = capsys.readouterr().out
    assert "Developer Portal" in out and "Reset Token" in out
    # the getpass prompt names Discord, not BotFather — a real bug in the shared helper otherwise
    assert prompts == ["Paste your Discord bot token: "]


def test_discord_interactive_empty_token_exits_2(monkeypatch, capsys) -> None:
    monkeypatch.setattr(connect_cli.getpass, "getpass", lambda *a, **k: "")
    rc = connect_cli.discord_bind_flow(9999, "mcp-tok", interactive=True)
    assert rc == 2
    assert "no token entered" in capsys.readouterr().err


def test_discord_interactive_getpass_interrupt_exits_2(monkeypatch, capsys) -> None:
    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(connect_cli.getpass, "getpass", boom)
    rc = connect_cli.discord_bind_flow(9999, "mcp-tok", interactive=True)
    assert rc == 2


def test_discord_autodetects_interactive_from_isatty(
    monkeypatch, stub_discord_daemon, capsys
) -> None:
    monkeypatch.setattr(connect_cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(connect_cli.getpass, "getpass", lambda *a, **k: _DISCORD_TOKEN)
    rc = connect_cli.discord_bind_flow(9999, "mcp-tok")
    assert rc == 0
    assert "Developer Portal" in capsys.readouterr().out


def test_discord_bad_token_format_exits_2(monkeypatch, stub_discord_daemon, capsys) -> None:
    _pipe_stdin(monkeypatch, _DISCORD_TOKEN + "\n")
    stub_discord_daemon["post_status"] = 400
    stub_discord_daemon["post_body"] = {"error": "bad_token_format"}
    rc = connect_cli.discord_bind_flow(9999, "mcp-tok", interactive=False)
    assert rc == 2
    assert "token rejected (bad_token_format)" in capsys.readouterr().err


def test_discord_daemon_unreachable_exits_3(monkeypatch, capsys) -> None:
    _pipe_stdin(monkeypatch, _DISCORD_TOKEN + "\n")

    def boom(*a, **k):
        raise control.DaemonUnreachable("connection refused")

    monkeypatch.setattr(control, "post", boom)
    rc = connect_cli.discord_bind_flow(9999, "mcp-tok", interactive=False)
    assert rc == 3
    assert "could not reach the daemon" in capsys.readouterr().err


def test_discord_timeout_exits_1_with_discord_specific_wording(
    monkeypatch, stub_discord_daemon, capsys
) -> None:
    _pipe_stdin(monkeypatch, _DISCORD_TOKEN + "\n")
    stub_discord_daemon["bound"] = False  # never binds
    rc = connect_cli.discord_bind_flow(9999, "mcp-tok", interactive=False, timeout=0)
    assert rc == 1
    err = capsys.readouterr().err
    assert "Timed out waiting for the DM" in err
    assert "sellee connect discord" in err
    assert "/start" not in err  # Telegram's own timeout wording never leaks into this flow


def test_discord_prints_the_invite_url_and_nonce(monkeypatch, stub_discord_daemon, capsys) -> None:
    _pipe_stdin(monkeypatch, _DISCORD_TOKEN + "\n")
    rc = connect_cli.discord_bind_flow(9999, "mcp-tok", interactive=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "https://discord.com/oauth2/authorize?client_id=999" in out
    assert "n0" in out  # the nonce the seller must DM back


def test_discord_prints_a_terminal_qr_above_the_invite(
    monkeypatch, stub_discord_daemon, capsys
) -> None:
    _pipe_stdin(monkeypatch, _DISCORD_TOKEN + "\n")
    connect_cli.discord_bind_flow(9999, "mcp-tok", interactive=False)
    out = capsys.readouterr().out
    qr_block = out.split("validated.", 1)[1].split("Scan the code", 1)[0]
    assert "█" in qr_block
    assert "\x1b" not in out


def test_discord_interactive_default_timeout_is_300(
    monkeypatch, stub_discord_daemon, capsys
) -> None:
    monkeypatch.setattr(connect_cli.getpass, "getpass", lambda *a, **k: _DISCORD_TOKEN)
    connect_cli.discord_bind_flow(9999, "mcp-tok", interactive=True)
    assert "up to 300s" in capsys.readouterr().out


def test_discord_piped_default_timeout_is_120(monkeypatch, stub_discord_daemon, capsys) -> None:
    _pipe_stdin(monkeypatch, _DISCORD_TOKEN + "\n")
    connect_cli.discord_bind_flow(9999, "mcp-tok", interactive=False)
    assert "up to 120s" in capsys.readouterr().out


def test_discord_token_never_printed_to_stdout(monkeypatch, stub_discord_daemon, capsys) -> None:
    monkeypatch.setattr(connect_cli.getpass, "getpass", lambda *a, **k: _DISCORD_TOKEN)
    connect_cli.discord_bind_flow(9999, "mcp-tok", interactive=True)
    captured = capsys.readouterr()
    assert _DISCORD_TOKEN not in captured.out and _DISCORD_TOKEN not in captured.err


# --- run(): connect_command dispatch ------------------------------------------------------------


def test_run_dispatches_discord_status_to_print_status(monkeypatch, capsys) -> None:
    monkeypatch.setattr(control, "require_token", lambda: "mcp-tok")
    monkeypatch.setattr(connect_cli.config, "load", lambda path=None: Config())
    monkeypatch.setattr(
        control,
        "get",
        lambda *a, **k: (
            200,
            {"adapter": "discord", "bound": False, "awaiting_bind": True, "bot_username": "b"},
        ),
    )

    class Args:
        connect_command = "discord"
        status = True

    assert connect_cli.run(Args()) == 1
    assert "awaiting a DM for @b" in capsys.readouterr().out


def _status_args(command: str):
    class Args:
        connect_command = command
        status = True

    return Args()


@pytest.mark.parametrize(
    ("asked", "holder", "holder_name"),
    [("discord", "telegram", "Telegram"), ("telegram", "discord", "Discord")],
)
def test_status_never_reports_the_other_providers_binding(
    monkeypatch, capsys, asked, holder, holder_name
) -> None:
    """One channel binds at a time, so the status route answers for whichever provider holds it.
    Asking about the other one must not read back that holder's bot as this provider's."""
    monkeypatch.setattr(control, "require_token", lambda: "mcp-tok")
    monkeypatch.setattr(connect_cli.config, "load", lambda path=None: Config())
    monkeypatch.setattr(
        control,
        "get",
        lambda *a, **k: (200, {"adapter": holder, "bound": True, "bot_username": "otherbot"}),
    )

    assert connect_cli.run(_status_args(asked)) == 1
    out = capsys.readouterr().out
    assert f"not connected — {holder_name} holds the channel" in out
    assert "otherbot" not in out


def test_run_dispatches_discord_connect_to_discord_bind_flow(monkeypatch) -> None:
    monkeypatch.setattr(control, "require_token", lambda: "mcp-tok")
    monkeypatch.setattr(connect_cli.config, "load", lambda path=None: Config())
    calls = []
    monkeypatch.setattr(connect_cli, "discord_bind_flow", lambda *a, **k: calls.append((a, k)) or 0)

    class Args:
        connect_command = "discord"
        status = False
        timeout = 42

    assert connect_cli.run(Args()) == 0
    assert calls and calls[0][1]["timeout"] == 42


# --- marketplace sign-in ---------------------------------------------------------------------


@pytest.fixture
def stub_market_daemon(monkeypatch):
    """Stub the connect-market POST and the login probe. `state` drives both. The window raise,
    the deployment check, and the config read are stubbed too, so a test drives them by hand."""
    calls = {
        "posts": [],
        "probes": [],
        "holds": [],
        "state": "logged_in",
        "post_status": 200,
        "raised": [],
        "raise_result": True,
    }

    def fake_post(port, token, route, body, **kwargs):
        # The login probe is a POST too (it navigates the shared tab), recorded separately so
        # tests can tell "asked to connect" from "asked whether we are in".
        if route == "/control/market-login":
            calls["probes"].append((route, body))
            return calls.get("probe_status", 200), {"state": calls["state"]}
        if route in ("/control/browser-hold", "/control/browser-release"):
            # Claims on the shared tab, recorded apart from the navigations: a test about signing
            # in should be able to say the lanes were let back in, without that showing up as
            # another page the flow opened.
            calls["holds"].append((route, body.get("holder")))
            return 200, {}
        calls["posts"].append((route, body))
        return calls["post_status"], calls.get(
            "post_body",
            {"market": body["market"], "url": "https://www.carousell.sg/", "state": calls["state"]},
        )

    def fake_raise(port):
        calls["raised"].append(port)
        return calls["raise_result"]

    monkeypatch.setattr(control, "post", fake_post)
    monkeypatch.setattr(connect_cli.foreground, "raise_window", fake_raise)
    # Faked rather than read, so these tests are about the flow on a machine that can raise a
    # window whichever machine runs the suite.
    monkeypatch.setattr(connect_cli.foreground, "is_supported", lambda: True)
    monkeypatch.setattr(connect_cli.deployment, "is_container", lambda env=None: False)
    monkeypatch.setattr(connect_cli.config, "load", lambda: Config(chrome_cdp_port=9333))
    return calls


def test_an_already_signed_in_market_exits_0_without_asking(
    monkeypatch, stub_market_daemon, capsys
) -> None:
    rc = connect_cli.market_flow(9999, "mcp-tok", "carousell", interactive=True)
    assert rc == 0
    assert stub_market_daemon["probes"] == []  # no second probe, nothing to wait for
    assert "Signed in to Carousell" in capsys.readouterr().out


def test_a_signed_out_market_waits_then_re_probes(monkeypatch, stub_market_daemon, capsys) -> None:
    stub_market_daemon["state"] = "logged_out"
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "")

    rc = connect_cli.market_flow(9999, "mcp-tok", "carousell", interactive=True)

    assert rc == 1
    assert len(stub_market_daemon["probes"]) == 1
    assert "sign in there. I never sign in for you" in capsys.readouterr().out
    assert prompts  # it did wait for the person


def test_an_unknown_probe_is_not_reported_as_signed_out(
    monkeypatch, stub_market_daemon, capsys
) -> None:
    stub_market_daemon["state"] = "unknown"
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    rc = connect_cli.market_flow(9999, "mcp-tok", "carousell", interactive=True)
    assert rc == 1
    assert "I'll confirm the first time I list" in capsys.readouterr().out


def test_a_non_interactive_run_opens_the_tab_and_does_not_block(
    monkeypatch, stub_market_daemon, capsys
) -> None:
    stub_market_daemon["state"] = "logged_out"
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": pytest.fail("must not prompt with no terminal")
    )
    assert connect_cli.market_flow(9999, "mcp-tok", "carousell", interactive=False) == 1


def test_an_unavailable_browser_exits_3_with_the_hint(
    monkeypatch, stub_market_daemon, capsys
) -> None:
    stub_market_daemon["post_status"] = 503
    stub_market_daemon["post_body"] = {"error": "browser_unavailable", "detail": "start Chrome…"}
    assert connect_cli.market_flow(9999, "mcp-tok", "carousell", interactive=False) == 3
    assert "start Chrome" in capsys.readouterr().err


def test_a_busy_browser_says_try_again_rather_than_failing_opaquely(
    monkeypatch, stub_market_daemon, capsys
) -> None:
    stub_market_daemon["post_status"] = 409
    stub_market_daemon["post_body"] = {
        "error": "browser_busy",
        "detail": "a pass is using the browser right now",
    }
    assert connect_cli.market_flow(9999, "mcp-tok", "carousell", interactive=False) == 3
    err = capsys.readouterr().err
    assert "a pass is using the browser" in err
    assert "try again" in err


def test_a_signed_out_market_raises_the_agents_chrome_window(stub_market_daemon, capsys) -> None:
    stub_market_daemon["post_body"] = {
        "market": "carousell",
        "url": "https://www.carousell.sg/",
        "state": "logged_out",
        "raise_window": True,
    }
    connect_cli.market_flow(9999, "mcp-tok", "carousell", interactive=False)
    assert stub_market_daemon["raised"] == [9333]  # the configured CDP port finds the window
    assert "Can't spot it" not in capsys.readouterr().out  # a raised window is its own message


def test_an_older_daemon_without_the_flag_still_raises(stub_market_daemon) -> None:
    """A response with no raise_window field reads as the setting's default — raise."""
    stub_market_daemon["state"] = "logged_out"
    connect_cli.market_flow(9999, "mcp-tok", "carousell", interactive=False)
    assert stub_market_daemon["raised"] == [9333]


def test_a_window_that_would_not_come_forward_gets_a_hint_not_a_failure(
    stub_market_daemon, capsys
) -> None:
    stub_market_daemon["state"] = "logged_out"
    stub_market_daemon["raise_result"] = False
    rc = connect_cli.market_flow(9999, "mcp-tok", "carousell", interactive=False)
    assert rc == 1  # the exit code never depends on the raise
    assert "Can't spot it" in capsys.readouterr().out


def test_a_seller_who_chose_background_mode_gets_no_raise_and_a_pointer(
    stub_market_daemon, capsys
) -> None:
    stub_market_daemon["post_body"] = {
        "market": "carousell",
        "url": "https://www.carousell.sg/",
        "state": "logged_out",
        "raise_window": False,
    }
    connect_cli.market_flow(9999, "mcp-tok", "carousell", interactive=False)
    assert stub_market_daemon["raised"] == []
    out = capsys.readouterr().out
    assert "background" in out
    assert "/sellee" in out  # the way back to the toggle travels with the consequence


def test_an_os_that_cannot_raise_says_so_instead_of_hinting_at_a_failure(
    monkeypatch, stub_market_daemon, capsys
) -> None:
    """A permanent no-op must not read as "it didn't work this time" — and must not blame a
    setting the seller could change, or name macOS furniture the machine hasn't got."""
    stub_market_daemon["state"] = "logged_out"
    monkeypatch.setattr(connect_cli.foreground, "is_supported", lambda: False)
    connect_cli.market_flow(9999, "mcp-tok", "carousell", interactive=False)
    assert stub_market_daemon["raised"] == []
    out = capsys.readouterr().out
    assert "on this operating system" in out
    assert "Dock" not in out
    assert "your setting" not in out


def test_container_mode_points_at_the_sellers_own_chrome_and_never_raises(
    monkeypatch, stub_market_daemon, capsys
) -> None:
    stub_market_daemon["state"] = "logged_out"
    monkeypatch.setattr(connect_cli.deployment, "is_container", lambda env=None: True)
    connect_cli.market_flow(9999, "mcp-tok", "carousell", interactive=False)
    assert stub_market_daemon["raised"] == []
    assert "your own computer" in capsys.readouterr().out


def test_an_already_signed_in_market_never_touches_the_window(stub_market_daemon) -> None:
    connect_cli.market_flow(9999, "mcp-tok", "carousell", interactive=True)
    assert stub_market_daemon["raised"] == []  # no window is needed, so none is raised


def test_a_refused_enqueue_is_reported_not_a_traceback(monkeypatch, capsys) -> None:
    # control.post returns (status, body) for HTTP errors rather than raising, so a caller that
    # ignores the status reads a missing key out of an error body.
    from sellee import config as config_mod
    from sellee import pass_cli

    monkeypatch.setattr(control, "require_token", lambda: "tok")
    monkeypatch.setattr(config_mod, "load", lambda path=None: config_mod.Config())
    monkeypatch.setattr(control, "post", lambda *a, **k: (400, {"error": "no such item"}))

    class Args:
        pass_type = "publish"
        item = "itm_missing"
        market = None
        follow = False

    assert pass_cli.run(Args()) == 1
    assert "no such item" in capsys.readouterr().err


def test_a_refused_settings_decision_is_not_reported_as_done(monkeypatch, capsys) -> None:
    from sellee import settings_cli

    monkeypatch.setattr(control, "post", lambda *a, **k: (400, {"error": "unknown change id"}))
    assert settings_cli._decide(9999, "tok", "approve", "chg_nope") == 1
    assert "unknown change id" in capsys.readouterr().err
