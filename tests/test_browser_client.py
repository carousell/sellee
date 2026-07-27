"""The browser client over a real subprocess: handshake, tool round trip, typed errors, no retry,
and the response framing Playwright MCP actually uses."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from selly_agent.browser import chrome
from selly_agent.browser.client import (
    BrowserClient,
    BrowserToolError,
    BrowserTransportError,
    BrowserUnavailable,
    default_command,
    evaluate_result,
    sections,
)

FAKE = Path(__file__).parent / "fake_playwright_mcp.py"


@pytest.fixture
def make_client(tmp_path):
    """A client wired to the scripted fake server, so the transport under test is the real one."""
    clients = []

    def _make(script: dict, **kwargs):
        script_path = tmp_path / f"script-{len(clients)}.json"
        script_path.write_text(json.dumps(script))
        client = BrowserClient(
            command=[sys.executable, str(FAKE), str(script_path)],
            timeout_sec=kwargs.pop("timeout_sec", 10.0),
            **kwargs,
        )
        client.calls_log = tmp_path / f"script-{len(clients)}.json.calls"
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.close()


def tool_calls(client) -> list:
    """The tool calls the fake server saw, in order."""
    if not client.calls_log.exists():
        return []
    return [json.loads(line) for line in client.calls_log.read_text().splitlines() if line.strip()]


# --- response framing (pinned against the real server's rendering) -------------------------------


def test_sections_splits_the_markdown_response() -> None:
    text = "### Result\n[1, 2]\n### Ran Playwright code\n```js\nawait x();\n```"
    assert sections(text)["Result"] == "[1, 2]"
    assert "await x();" in sections(text)["Ran Playwright code"]


def test_evaluate_result_decodes_the_result_section() -> None:
    text = '### Result\n{\n  "state": "logged_in"\n}\n### Page\nhttps://x/'
    assert evaluate_result(text) == {"state": "logged_in"}


def test_evaluate_result_of_a_function_returning_nothing_is_none() -> None:
    assert evaluate_result("### Ran Playwright code\n```js\nawait x();\n```") is None


def test_evaluate_result_survives_a_fenced_section() -> None:
    assert evaluate_result('### Result\n```\n{"a": 1}\n```') == {"a": 1}


def test_an_undecodable_result_is_a_transport_error() -> None:
    with pytest.raises(BrowserTransportError, match="undecodable"):
        evaluate_result("### Result\nnot json at all")


# --- handshake + round trip ----------------------------------------------------------------------


def test_a_tool_call_round_trips_through_the_handshake(make_client) -> None:
    client = make_client({"tools": {"browser_evaluate": {"result": {"href": "https://x/"}}}})
    assert client.evaluate("() => window.location.href") == {"href": "https://x/"}


def test_the_tail_read_comes_back_as_a_real_structure(make_client) -> None:
    """The server JSON-encodes the function's return value, so a JS artifact hands back a list of
    bubbles rather than a string the caller has to parse itself."""
    bubbles = [{"text": "still available?", "side": "in", "y": 10}]
    client = make_client({"tools": {"browser_evaluate": {"result": bubbles}}})
    assert client.evaluate("() => []") == bubbles


def test_interleaved_notifications_are_skipped(make_client) -> None:
    """The fake emits a notification before every reply, as a real server does."""
    script = {"tools": {"browser_evaluate": {"result": 1}, "browser_tabs": {"text": "ok"}}}
    client = make_client(script)
    client.ensure_tab()
    assert client.evaluate("() => 1") == 1


# --- typed errors --------------------------------------------------------------------------------


def test_a_missing_binary_is_browser_unavailable(tmp_path) -> None:
    """Node absent must be its own error: the daemon keeps running with its browser lanes skipped,
    which is a different response from an action that failed."""
    client = BrowserClient(command=[str(tmp_path / "no-such-binary")])
    with pytest.raises(BrowserUnavailable, match="could not start"):
        client.call_tool("browser_navigate", {"url": "https://x/"})


def test_a_server_that_dies_at_startup_is_unavailable(make_client) -> None:
    client = make_client({"on_start": "die"})
    with pytest.raises(BrowserTransportError, match="exited"):
        client.evaluate("() => 1")


def test_a_failed_tool_is_a_tool_error_carrying_the_reason(make_client) -> None:
    client = make_client({"tools": {"browser_click": {"error": "no element matches selector"}}})
    with pytest.raises(BrowserToolError, match="no element matches selector"):
        client.call_tool("browser_click", {"target": "button.gone"})


def test_a_malformed_frame_is_a_transport_error(make_client) -> None:
    client = make_client({"malformed": ["browser_snapshot"], "tools": {"browser_snapshot": {}}})
    with pytest.raises(BrowserTransportError, match="unparseable"):
        client.call_tool("browser_snapshot", {})


def test_a_server_that_exits_mid_call_is_a_transport_error(make_client) -> None:
    client = make_client({"die_on": ["browser_navigate"], "tools": {"browser_navigate": {}}})
    with pytest.raises(BrowserTransportError, match="exited"):
        client.call_tool("browser_navigate", {"url": "https://x/"})


def test_the_client_never_retries_a_failed_call(make_client) -> None:
    """A hot retry against a marketplace is the anti-automation tell, so backing off is the lane's
    job — the client raises once and calls once."""
    script = {"tools": {"browser_click": [{"error": "first failure"}, {"text": "second call"}]}}
    client = make_client(script)
    with pytest.raises(BrowserToolError, match="first failure"):
        client.call_tool("browser_click", {"target": "x"})


# --- tab ownership -------------------------------------------------------------------------------


def test_the_client_opens_its_own_tab_once(make_client) -> None:
    """One tab, created by us and held for the client's lifetime — never selected by index, which
    renumbers whenever any tab opens or closes."""
    client = make_client(
        {
            "tools": {
                "browser_tabs": {"text": "### Open tabs\n- 0: about:blank"},
                "browser_navigate": {"text": "ok"},
            }
        }
    )
    client.navigate("https://www.carousell.sg/inbox/")
    client.navigate("https://www.carousell.sg/inbox/12/")
    calls = [call["tool"] for call in tool_calls(client)]
    assert calls.count("browser_tabs") == 1  # the second navigate reuses the tab
    assert [call["arguments"] for call in tool_calls(client) if call["tool"] == "browser_tabs"] == [
        {"action": "new"}
    ]
    assert "select" not in json.dumps(tool_calls(client))  # never re-selected by index


# --- the mutex -----------------------------------------------------------------------------------


def test_exclusive_serializes_a_multi_call_operation(make_client) -> None:
    """A send is navigate → locate → type → click → verify; the lane's read must not land in the
    middle of it, so a compound operation holds the client for its whole sequence."""
    import threading

    client = make_client({"tools": {"browser_evaluate": {"result": 1}}})
    order: list = []
    started = threading.Event()

    def reader():
        started.wait(timeout=5)
        client.evaluate("() => 1")
        order.append("read")

    thread = threading.Thread(target=reader)
    thread.start()
    with client.exclusive():
        started.set()
        # the reader is now blocked on the lock; both of these land before it
        client.evaluate("() => 1")
        order.append("send-a")
        client.evaluate("() => 1")
        order.append("send-b")
    thread.join(timeout=5)
    assert order == ["send-a", "send-b", "read"]


# --- the Chrome bring-up -------------------------------------------------------------------------


def test_the_readiness_probe_is_false_with_nothing_listening() -> None:
    # port 1 is privileged and never our Chrome, so this exercises the unreachable path
    assert chrome.is_ready(1, timeout_sec=0.2) is False


def test_the_launch_command_uses_the_agents_own_profile(xdg_tmp) -> None:
    from selly_agent import paths

    argv = chrome.launch_command(9222, chrome_bin="/bin/chrome")
    assert f"--user-data-dir={paths.browser_profile_dir()}" in argv
    assert "--remote-debugging-port=9222" in argv
    assert str(paths.browser_profile_dir()) != str(Path.home() / "Library")


def test_stale_singleton_locks_are_cleared(xdg_tmp) -> None:
    """A SIGKILLed Chrome leaves these behind and the next launch hangs on them."""
    from selly_agent import paths

    paths.ensure_data_dirs()
    for name in chrome.SINGLETON_LOCKS:
        (paths.browser_profile_dir() / name).write_text("")
    assert sorted(chrome.clear_stale_locks()) == sorted(chrome.SINGLETON_LOCKS)
    assert chrome.clear_stale_locks() == []  # idempotent


def test_the_bring_up_hint_names_the_port_and_the_command() -> None:
    hint = chrome.bring_up_hint(9333)
    assert "9333" in hint and "--remote-debugging-port=9333" in hint


def test_the_default_command_pins_the_cdp_endpoint() -> None:
    argv = default_command("http://127.0.0.1:9222")
    assert argv[:2] == ["npx", "--yes"]
    assert "--cdp-endpoint" in argv and "http://127.0.0.1:9222" in argv
