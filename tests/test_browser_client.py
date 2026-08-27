"""The browser client over a real subprocess: handshake, tool round trip, typed errors, no retry,
and the response framing Playwright MCP actually uses."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from sellee.browser import chrome
from sellee.browser.client import (
    PINNED_MCP_SPEC,
    BrowserClient,
    BrowserToolError,
    BrowserTransportError,
    BrowserUnavailable,
    default_command,
    ensure_available,
    evaluate_result,
    same_page,
    sections,
)

# The real probe, bound at import so the conftest guard's stub cannot reach it. These are the
# probe's own tests, so they are the ones that must call the real thing.
_real_is_ready = chrome.is_ready


@pytest.fixture(autouse=True)
def _throwaway_profile(xdg_tmp):
    """Every path to ensure_running reaches clear_stale_locks, which deletes the Singleton locks
    and the announced-port file out of the resolved profile directory. Unredirected, that is the
    developer's real agent profile — running this suite beside a live agent Chrome would delete
    its announcement and trick the daemon into a second launch on a live profile."""
    return xdg_tmp


FAKE = Path(__file__).parent / "fake_playwright_mcp.py"


@pytest.fixture(autouse=True)
def _fresh_launch_backoff():
    """The failed-launch backoff is module state; a test that exercised a failure must not quiet
    the launches of the tests after it."""
    chrome._last_failed_launch_ts = None
    yield
    chrome._last_failed_launch_ts = None


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


# Captured from a real @playwright/mcp against a real Chrome, not composed by hand — an earlier
# hand-written fixture had the void case wrong (it assumed the Result section would be absent, where
# the server actually writes a literal `undefined`), and the test passed while the code was broken.
_LIVE_RESULT_BODIES = [
    ("undefined", None),
    ("null", None),
    ("false", False),
    ("0", 0),
    ("[]", []),
    ('"hello"', "hello"),
    ('[{"text": "hi", "side": "in", "y": 157}]', [{"text": "hi", "side": "in", "y": 157}]),
]


@pytest.mark.parametrize(("body", "expected"), _LIVE_RESULT_BODIES)
def test_evaluate_result_matches_what_the_real_server_emits(body, expected) -> None:
    text = f"### Result\n{body}\n### Ran Playwright code\n```js\nawait page.evaluate('…');\n```"
    assert evaluate_result(text) == expected


def test_nothing_returned_is_none_and_stays_distinct_from_an_empty_list() -> None:
    """Three different answers that must not collapse into each other: a function that returned
    nothing (a literal `undefined`), one that returned null to abstain, and one that returned an
    empty list. Reading either of the first two as a failure would make an artifact that fell off
    its own end look unreadable; reading them as `[]` would make it look like good news."""
    assert evaluate_result("### Result\nundefined") is None
    assert evaluate_result("### Result\nnull") is None
    assert evaluate_result("### Ran Playwright code\n```js\nawait x();\n```") is None
    assert evaluate_result("### Result\n[]") == []


def test_evaluate_result_survives_a_fenced_section() -> None:
    assert evaluate_result('### Result\n```\n{"a": 1}\n```') == {"a": 1}


def test_an_undecodable_result_is_a_transport_error() -> None:
    with pytest.raises(BrowserTransportError, match="undecodable"):
        evaluate_result("### Result\nnot json at all")


# --- handshake + round trip ----------------------------------------------------------------------


def test_a_tool_call_round_trips_through_the_handshake(make_client) -> None:
    client = make_client({"tools": {"browser_evaluate": {"result": {"href": "https://x/"}}}})
    assert client.evaluate("() => window.location.href") == {"href": "https://x/"}


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
    """Absence and failure route differently: unavailable means skip-and-notify with the install
    hint, where a transport error feeds the blind counter. A server that never completed its
    handshake is the former."""
    client = make_client({"on_start": "die"})
    with pytest.raises(BrowserUnavailable, match="did not start"):
        client.evaluate("() => 1")


def test_a_missing_binary_is_unavailable_before_anything_spawns(monkeypatch) -> None:
    monkeypatch.setattr("sellee.browser.client.shutil.which", lambda _name: None)
    with pytest.raises(BrowserUnavailable, match="playwright_mcp_cmd"):
        ensure_available(["npx", "--yes", "@playwright/mcp"])


def test_a_present_binary_passes_the_availability_check(monkeypatch) -> None:
    monkeypatch.setattr("sellee.browser.client.shutil.which", lambda name: f"/usr/bin/{name}")
    ensure_available(["npx"])


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
    job — the client raises once and calls once. The one exception is a tab that reports modal
    state: a tab to replace rather than an action to repeat. See the tab-ownership tests."""
    script = {"tools": {"browser_click": [{"error": "first failure"}, {"text": "second call"}]}}
    client = make_client(script)
    with pytest.raises(BrowserToolError, match="first failure"):
        client.call_tool("browser_click", {"target": "x"})


# --- tab ownership -------------------------------------------------------------------------------

_URL = "https://www.carousell.sg/inbox/12/"


def test_the_client_opens_its_own_tab_once(make_client) -> None:
    """One tab, created by us and held for the client's lifetime. Reading and navigating never
    select by index — an index renumbers whenever any tab opens or closes — so only a send, which
    needs the tab visible for keys to land, ever pays that cost."""
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


# --- a tab that carries modal state --------------------------------------------------------------

# What the real server answers with while the tab it is pointed at has a dialog or a file chooser
# open. Only the tool that owns that state clears it, and a second server on the same Chrome records
# the same states on its own copy of every tab without ever clearing them — so a file chooser a
# publish pass opened and consumed refuses our reads on that tab from then on.
_MODAL_STATE = {"error": 'Error: Tool "browser_evaluate" does not handle the modal state.'}


def test_a_tab_that_reports_modal_state_is_given_up_for_a_fresh_one(make_client) -> None:
    """The recovery is a new tab, not a repair: the state outlives the page, so navigating does not
    clear it, and the tool that would clear it belongs to whoever opened the dialog. Left
    unrecovered, the market reads as unreadable on every tick until the daemon restarts."""
    client = make_client(
        {
            "tools": {
                "browser_tabs": {"text": "ok"},
                "browser_navigate": {"text": "ok"},
                "browser_evaluate": [_MODAL_STATE, {"result": {"conversations": []}}],
            }
        }
    )
    client.navigate(_URL)
    assert client.evaluate("() => 1") == {"conversations": []}
    assert [(call["tool"], call["arguments"]) for call in tool_calls(client)] == [
        ("browser_tabs", {"action": "new"}),
        ("browser_navigate", {"url": _URL}),
        ("browser_evaluate", {"function": "() => 1"}),
        ("browser_tabs", {"action": "new"}),
        ("browser_navigate", {"url": _URL}),  # the replacement resumes on the page we were reading
        ("browser_evaluate", {"function": "() => 1"}),
    ]


def test_a_replacement_that_also_reports_modal_state_fails_rather_than_looping(make_client) -> None:
    """One recovery per call. A tab we have just opened cannot carry modal state, so a second
    refusal is something else entirely and belongs to the caller, not to another new tab."""
    client = make_client(
        {
            "tools": {
                "browser_tabs": {"text": "ok"},
                "browser_navigate": {"text": "ok"},
                "browser_evaluate": [_MODAL_STATE, _MODAL_STATE, {"result": "unreached"}],
            }
        }
    )
    client.navigate(_URL)
    with pytest.raises(BrowserToolError, match="modal state"):
        client.evaluate("() => 1")
    tools = [call["tool"] for call in tool_calls(client)]
    assert tools.count("browser_tabs") == 2  # the tab we opened, and its one replacement
    assert tools.count("browser_evaluate") == 2


def test_a_refusal_before_any_navigation_takes_a_tab_and_nothing_else(make_client) -> None:
    """Nothing to resume: a read that has not navigated anywhere has no page to be sent back to, and
    inventing one would be navigating on a caller's behalf."""
    client = make_client(
        {
            "tools": {
                "browser_tabs": {"text": "ok"},
                "browser_evaluate": [_MODAL_STATE, {"result": True}],
            }
        }
    )
    assert client.evaluate("() => 1") is True
    assert [call["tool"] for call in tool_calls(client)] == [
        "browser_evaluate",
        "browser_tabs",
        "browser_evaluate",
    ]


# --- bringing our own tab forward ----------------------------------------------------------------

_TAB_LIST = {"text": "### Open tabs\n- 0: [a](x)\n- 1: (current) [b](y)"}


def test_an_already_visible_tab_is_left_alone(make_client) -> None:
    """The steady state on the agent's own Chrome. Selecting anyway would pull the window in front
    of whatever the seller is doing, once per send, for nothing."""
    client = make_client(
        {"tools": {"browser_evaluate": {"result": {"visible": True, "url": _URL}}}}
    )
    client.ensure_frontmost(_URL)
    assert [call["tool"] for call in tool_calls(client)] == ["browser_evaluate"]


def test_a_hidden_tab_is_selected_and_then_confirmed(make_client) -> None:
    """A background tab takes the text and drops the key that sends it, with no error anywhere."""
    client = make_client(
        {
            "tools": {
                "browser_evaluate": [
                    {"result": {"visible": False, "url": _URL}},
                    {"result": {"visible": True, "url": _URL}},
                ],
                "browser_tabs": [_TAB_LIST, {"text": "ok"}],
            }
        }
    )
    client.ensure_frontmost(_URL)
    tabs = [call["arguments"] for call in tool_calls(client) if call["tool"] == "browser_tabs"]
    assert tabs == [{"action": "list"}, {"action": "select", "index": 1}]


def test_selecting_a_tab_that_is_not_ours_raises_and_gives_up_the_tab(make_client) -> None:
    """Selecting repoints every later call at whatever index was chosen, and indices renumber as
    tabs open and close. Landing on someone else's page must not become typing into it."""
    client = make_client(
        {
            "tools": {
                "browser_evaluate": [
                    {"result": {"visible": False, "url": _URL}},
                    {"result": {"visible": True, "url": "https://www.carousell.sg/sell/"}},
                ],
                "browser_tabs": [{"text": "ok"}, _TAB_LIST, {"text": "ok"}],
                "browser_navigate": {"text": "ok"},
            }
        }
    )
    client.navigate(_URL)  # takes our own tab, so there is a handle to give up
    with pytest.raises(BrowserToolError, match="landed on"):
        client.ensure_frontmost(_URL)
    assert client._tab_opened is False  # noqa: SLF001 — the handle is the thing under test


def test_a_tab_that_stays_hidden_is_an_error_but_stays_ours(make_client) -> None:
    """Still our tab, just not visible — so the handle is kept and only the send is refused. Giving
    the tab up here would abandon a healthy one on every failure."""
    client = make_client(
        {
            "tools": {
                "browser_evaluate": {"result": {"visible": False, "url": _URL}},
                "browser_tabs": [{"text": "ok"}, _TAB_LIST, {"text": "ok"}],
                "browser_navigate": {"text": "ok"},
            }
        }
    )
    client.navigate(_URL)
    with pytest.raises(BrowserToolError, match="still hidden"):
        client.ensure_frontmost(_URL)
    assert client._tab_opened is True  # noqa: SLF001 — the handle is the thing under test


def test_the_visibility_read_after_selecting_waits_for_the_change(make_client) -> None:
    """Chrome tells the renderer it became visible asynchronously, so reading the state straight
    after selecting reports the old value and a healthy tab looks like it would not come forward."""
    client = make_client(
        {
            "tools": {
                "browser_evaluate": [
                    {"result": {"visible": False, "url": _URL}},
                    {"result": {"visible": True, "url": _URL}},
                ],
                "browser_tabs": [_TAB_LIST, {"text": "ok"}],
            }
        }
    )
    client.ensure_frontmost(_URL)
    functions = [
        call["arguments"]["function"]
        for call in tool_calls(client)
        if call["tool"] == "browser_evaluate"
    ]
    assert "visibilitychange" not in functions[0]  # the first look is immediate
    assert "visibilitychange" in functions[1]  # the second waits on the event itself


def test_a_server_that_names_no_current_tab_is_an_error_not_a_guess(make_client) -> None:
    client = make_client(
        {
            "tools": {
                "browser_evaluate": {"result": {"visible": False, "url": _URL}},
                "browser_tabs": {"text": "### Open tabs\n- 0: [a](x)\n- 1: [b](y)"},
            }
        }
    )
    with pytest.raises(BrowserToolError, match="no current tab"):
        client.ensure_frontmost(_URL)


# --- watch mode: the tab follows the work ---------------------------------------------------------


def test_a_navigation_leaves_the_tab_where_it_is_by_default(make_client) -> None:
    """The seller is not watching, so a read costs one navigate and takes nobody's foreground."""
    client = make_client(
        {"tools": {"browser_tabs": {"text": "ok"}, "browser_navigate": {"text": "ok"}}}
    )
    client.navigate(_URL)
    assert [call["tool"] for call in tool_calls(client)] == ["browser_tabs", "browser_navigate"]


def test_watching_brings_our_tab_forward_after_a_navigation(make_client) -> None:
    client = make_client(
        {
            "tools": {
                "browser_tabs": [{"text": "ok"}, _TAB_LIST, {"text": "ok"}],
                "browser_navigate": {"text": "ok"},
                "browser_evaluate": [
                    {"result": {"visible": False, "url": _URL}},
                    {"result": {"visible": True, "url": _URL}},
                ],
            }
        }
    )
    client.set_follow(True)
    client.navigate(_URL)
    tabs = [call["arguments"] for call in tool_calls(client) if call["tool"] == "browser_tabs"]
    assert {"action": "select", "index": 1} in tabs


def test_watching_costs_one_look_when_the_tab_is_already_in_front(make_client) -> None:
    """The steady state once the seller has the window up: no select, no window shuffling."""
    client = make_client(
        {
            "tools": {
                "browser_tabs": {"text": "ok"},
                "browser_navigate": {"text": "ok"},
                "browser_evaluate": {"result": {"visible": True, "url": _URL}},
            }
        }
    )
    client.set_follow(True)
    client.navigate(_URL)
    assert [call["tool"] for call in tool_calls(client)] == [
        "browser_tabs",
        "browser_navigate",
        "browser_evaluate",
    ]


def test_a_tab_that_will_not_come_forward_never_fails_the_navigation(make_client) -> None:
    """Watch mode is a view onto the work, not part of it. And the recovery is load-bearing: a
    select that landed elsewhere repoints every later call, so the caller's next read would be
    against a stranger's page unless our own tab is put back on the page it asked for."""
    client = make_client(
        {
            "tools": {
                "browser_tabs": [{"text": "ok"}, _TAB_LIST, {"text": "ok"}, {"text": "ok"}],
                "browser_navigate": {"text": "ok"},
                "browser_evaluate": [
                    {"result": {"visible": False, "url": _URL}},
                    {"result": {"visible": True, "url": "https://www.carousell.sg/sell/"}},
                ],
            }
        }
    )
    client.set_follow(True)
    client.navigate(_URL)  # no raise
    calls = [call["tool"] for call in tool_calls(client)]
    assert calls[-2:] == ["browser_tabs", "browser_navigate"]  # a fresh tab, back on our page
    assert client._tab_opened is True  # noqa: SLF001 — the replacement handle is the thing at issue


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        (_URL, _URL, True),
        (_URL, _URL.rstrip("/"), True),  # a navigation may drop or add the trailing slash
        (_URL, _URL + "?from=inbox", True),
        (_URL, _URL + "#top", True),
        (_URL, "https://www.carousell.sg/inbox/13/", False),
        (_URL, "https://www.carousell.sg/inbox/", False),
        ("", _URL, False),
    ],
)
def test_same_page_ignores_only_what_a_navigation_may_change(left, right, same) -> None:
    assert same_page(left, right) is same


# --- the Chrome bring-up -------------------------------------------------------------------------


def test_the_readiness_probe_is_false_with_nothing_listening() -> None:
    # port 1 is privileged and never our Chrome, so this exercises the unreachable path
    assert _real_is_ready(1, timeout_sec=0.2) is False


def test_the_launch_command_uses_the_agents_own_profile(xdg_tmp) -> None:
    from sellee import paths

    argv = chrome.launch_command(9222, chrome_bin="/bin/chrome")
    assert f"--user-data-dir={paths.browser_profile_dir()}" in argv
    assert "--remote-debugging-port=9222" in argv
    assert str(paths.browser_profile_dir()) != str(Path.home() / "Library")


def test_an_unpinned_launch_lets_chrome_choose_the_port(xdg_tmp) -> None:
    """A fixed port is one a local process can bind before Chrome does, and one another local user
    can guess. Chrome picking it means neither: nothing binds a port that does not exist yet, and
    the choice is announced only inside a 0700 profile."""
    assert "--remote-debugging-port=0" in chrome.launch_command(None, chrome_bin="/bin/chrome")


def test_a_pinned_launch_still_asks_for_that_exact_port(xdg_tmp) -> None:
    """A seller who pinned a port meant it — the container's forwarder is agreed on a number."""
    assert "--remote-debugging-port=9333" in chrome.launch_command(9333, chrome_bin="/bin/chrome")


def test_the_launch_command_keeps_a_covered_window_out_of_the_hidden_state(xdg_tmp) -> None:
    """A window the seller has covered with another app otherwise counts as hidden, and a hidden tab
    is one a send has to raise before its keys will land."""
    assert "--disable-backgrounding-occluded-windows" in chrome.launch_command(9222)


def test_stale_singleton_locks_are_cleared(xdg_tmp) -> None:
    """A SIGKILLed Chrome leaves these behind and the next launch hangs on them."""
    from sellee import paths

    paths.ensure_data_dirs()
    for name in chrome.SINGLETON_LOCKS:
        (paths.browser_profile_dir() / name).write_text("")
    assert sorted(chrome.clear_stale_locks()) == sorted(chrome.SINGLETON_LOCKS)
    assert chrome.clear_stale_locks() == []  # idempotent


def test_a_stale_announced_port_is_cleared_rather_than_trusted(xdg_tmp) -> None:
    """A killed Chrome leaves its port behind. Believing it aims the probe at a port this Chrome no
    longer holds — and which anything else on the machine may have taken since."""
    from sellee import paths

    paths.ensure_data_dirs()
    _write_active_port(45123)
    assert chrome.active_port() == 45123
    assert chrome.ACTIVE_PORT_FILE in chrome.clear_stale_locks()
    assert chrome.active_port() is None


# --- the announced port --------------------------------------------------------------------------


def _write_active_port(port, ws_path: str = "/devtools/browser/abc-123") -> None:
    from sellee import paths

    (paths.browser_profile_dir() / chrome.ACTIVE_PORT_FILE).write_text(f"{port}\n{ws_path}\n")


def test_the_announced_port_is_read_from_the_profile(xdg_tmp) -> None:
    """Chrome writes the port it bound on line 1 and its browser WebSocket path on line 2."""
    from sellee import paths

    paths.ensure_data_dirs()
    _write_active_port(45123)
    assert chrome.active_port() == 45123


@pytest.mark.parametrize(
    "text",
    ["", "\n", "not-a-port\n/devtools/browser/x", "0\n/devtools/browser/x", "99999\n"],
)
def test_an_unreadable_announcement_reads_as_no_chrome(xdg_tmp, text) -> None:
    """A file we did not write or cannot parse must not become a port we then dial."""
    from sellee import paths

    paths.ensure_data_dirs()
    (paths.browser_profile_dir() / chrome.ACTIVE_PORT_FILE).write_text(text)
    assert chrome.active_port() is None


def test_a_missing_announcement_reads_as_no_chrome(xdg_tmp) -> None:
    from sellee import paths

    paths.ensure_data_dirs()
    assert chrome.active_port() is None


def test_a_pinned_port_wins_over_the_announced_one(xdg_tmp) -> None:
    """The pin is an agreement with something that cannot read this file — a container's forwarder,
    or a Chrome someone started by hand — so it is not the file's to override."""
    from sellee import paths

    paths.ensure_data_dirs()
    _write_active_port(45123)
    assert chrome.resolve_port(9333) == 9333
    assert chrome.resolve_port(None) == 45123


def test_with_no_pin_and_no_chrome_the_endpoint_falls_back_to_the_documented_port(xdg_tmp) -> None:
    """The callers that only build an endpoint need a number. It is the same number the by-hand
    instruction names, so a Chrome started that way is the one they then reach."""
    from sellee import paths

    paths.ensure_data_dirs()
    assert chrome.resolve_port(None) == chrome.DEFAULT_CDP_PORT


# --- whose Chrome is answering -------------------------------------------------------------------


class _Answer:
    """A loopback responder standing in for whatever bound the port first."""

    def __init__(self, payload) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self):
        return self._body


def _answering(monkeypatch, payload) -> None:
    monkeypatch.setattr(chrome.urllib.request, "urlopen", lambda url, **kw: _Answer(payload))


_CHROME_VERSION = {
    "Browser": "Chrome/140.0.7339.16",
    "webSocketDebuggerUrl": "ws://127.0.0.1:45123/devtools/browser/abc-123",
}


def test_the_probe_accepts_the_chrome_that_announced_this_port(xdg_tmp, monkeypatch) -> None:
    from sellee import paths

    paths.ensure_data_dirs()
    _write_active_port(45123)
    _answering(monkeypatch, _CHROME_VERSION)
    assert _real_is_ready(45123) is True


def test_the_probe_rejects_a_responder_that_is_not_chrome(xdg_tmp, monkeypatch) -> None:
    """Parseable JSON on the port used to mean "our Chrome is up". Anything running as this user
    can bind a loopback port and serve some; taken for Chrome, it is handed the agent's browser
    session — free to feed the agent fabricated pages and read back whatever it types into them."""
    from sellee import paths

    paths.ensure_data_dirs()
    _write_active_port(45123)
    for payload in (
        {"ok": True},
        [],
        {"Browser": "MyLittleProxy/1.0"},
        {"Browser": 9},
        # Right shape, wrong instance: it never saw the path Chrome announced for this port.
        {"Browser": "Chrome/140.0.7339.16", "webSocketDebuggerUrl": "ws://127.0.0.1:45123/x"},
        {"Browser": "Chrome/140.0.7339.16"},
    ):
        _answering(monkeypatch, payload)
        assert _real_is_ready(45123) is False, payload


def test_the_probe_still_answers_where_chrome_announced_nothing_here(xdg_tmp, monkeypatch) -> None:
    """In a container the announcement is on the seller's own machine, so there is no path to
    cross-check — the Browser field is the whole check, and it has to keep working."""
    from sellee import paths

    paths.ensure_data_dirs()
    _answering(monkeypatch, {"Browser": "Chrome/140.0.7339.16"})
    assert _real_is_ready(9222) is True
    _answering(monkeypatch, {"Browser": "MyLittleProxy/1.0"})
    assert _real_is_ready(9222) is False


def test_ensure_running_launches_nothing_when_chrome_already_answers(monkeypatch) -> None:
    """Two Chromes cannot share the profile, so a live port is the end of it."""
    launches = []
    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: True)
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda *a, **kw: launches.append(a))

    assert chrome.ensure_running(9222) == (chrome.READY, 9222)
    assert launches == []


def test_ensure_running_starts_chrome_and_waits_for_the_port(xdg_tmp, monkeypatch) -> None:
    from sellee import paths

    paths.ensure_data_dirs()
    (paths.browser_profile_dir() / chrome.SINGLETON_LOCKS[0]).write_text("")
    answers = iter([False, False, True])
    launched = {}

    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: next(answers))
    monkeypatch.setattr(chrome, "_LAUNCH_POLL_SEC", 0.0)
    monkeypatch.setattr(
        chrome.subprocess, "Popen", lambda argv, **kw: launched.update(argv=argv, kw=kw)
    )

    assert chrome.ensure_running(9222, chrome_bin="/bin/chrome") == (chrome.LAUNCHED, 9222)
    assert launched["argv"][0] == "/bin/chrome"
    # Its own session: the daemon exiting, or a pass group being killed, must not take the seller's
    # browser with it.
    assert launched["kw"]["start_new_session"] is True
    # The lock only goes once the probe has said nobody is answering.
    assert not (paths.browser_profile_dir() / chrome.SINGLETON_LOCKS[0]).exists()


def test_ensure_running_reports_unavailable_when_chrome_never_answers(monkeypatch) -> None:
    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: False)
    monkeypatch.setattr(chrome, "_LAUNCH_POLL_SEC", 0.0)
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda argv, **kw: None)

    assert chrome.ensure_running(9222, chrome_bin="/bin/chrome", wait_sec=0.01) == (
        chrome.UNAVAILABLE,
        None,
    )


def test_ensure_running_reports_unavailable_when_the_binary_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: False)

    def _boom(argv, **kw):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(chrome.subprocess, "Popen", _boom)
    assert chrome.ensure_running(9222, chrome_bin="/nope/chrome") == (chrome.UNAVAILABLE, None)


def test_an_unpinned_launch_reports_the_port_chrome_chose(xdg_tmp, monkeypatch) -> None:
    from sellee import paths

    paths.ensure_data_dirs()

    def _launch(argv, **kw):
        _write_active_port(45123)

    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: True)
    monkeypatch.setattr(chrome, "_LAUNCH_POLL_SEC", 0.0)
    monkeypatch.setattr(chrome.subprocess, "Popen", _launch)

    assert chrome.ensure_running(None, chrome_bin="/bin/chrome") == (chrome.LAUNCHED, 45123)


def test_an_unpinned_chrome_that_announces_nothing_is_unavailable(xdg_tmp, monkeypatch) -> None:
    """With no announcement there is no port to probe, so the wait ends in UNAVAILABLE rather than
    in a guess. Nothing is dialled at all — a port we only guessed is a port something else may be
    sitting on."""
    from sellee import paths

    paths.ensure_data_dirs()
    probed = []
    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: probed.append(port) or True)
    monkeypatch.setattr(chrome, "_LAUNCH_POLL_SEC", 0.0)
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda argv, **kw: None)

    assert chrome.ensure_running(None, chrome_bin="/bin/chrome", wait_sec=0.01) == (
        chrome.UNAVAILABLE,
        None,
    )
    assert probed == []


def test_an_unpinned_launch_does_not_trust_a_dead_chromes_port(xdg_tmp, monkeypatch) -> None:
    """A killed Chrome's announcement outlives it, naming a port nothing holds any more. The
    bring-up clears it rather than carrying it forward, and reports the port the new Chrome
    chose."""
    from sellee import paths

    paths.ensure_data_dirs()
    _write_active_port(45123)
    cleared = []

    def _launch(argv, **kw):
        cleared.append(chrome.active_port())
        _write_active_port(46001)

    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: port == 46001)
    monkeypatch.setattr(chrome, "_LAUNCH_POLL_SEC", 0.0)
    monkeypatch.setattr(chrome.subprocess, "Popen", _launch)

    assert chrome.ensure_running(None, chrome_bin="/bin/chrome") == (chrome.LAUNCHED, 46001)
    assert cleared == [None]  # the dead Chrome's port went before the new one started


def test_two_concurrent_callers_launch_one_chrome(xdg_tmp, monkeypatch) -> None:
    """Two lanes can want the browser in the same window. Without the launch lock both would see a
    silent port, both would clear the profile locks, and both would launch — two Chromes contending
    one profile. The loser must instead find the port answering and launch nothing."""
    import threading

    from sellee import paths

    paths.ensure_data_dirs()
    up = threading.Event()
    launches = []

    def _launch(argv, **kw):
        launches.append(argv)
        up.set()

    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: up.is_set())
    monkeypatch.setattr(chrome, "_LAUNCH_POLL_SEC", 0.0)
    monkeypatch.setattr(chrome.subprocess, "Popen", _launch)

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(chrome.ensure_running(9222)))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(launches) == 1
    assert sorted(results) == [(chrome.LAUNCHED, 9222), (chrome.READY, 9222)]


def test_a_live_port_clears_the_failed_launch_backoff(monkeypatch) -> None:
    """A port that answers is evidence of the thing the backoff infers against, so it ends the
    window early: a seller who starts Chrome by hand and later closes it gets a launch, not a
    can't-drive-a-browser refusal for the remaining minutes."""
    launches = []
    monkeypatch.setattr(chrome, "_LAUNCH_POLL_SEC", 0.0)
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda argv, **kw: launches.append(argv))

    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: False)
    assert chrome.ensure_running(9222, chrome_bin="/bin/chrome", wait_sec=0.01) == (
        chrome.UNAVAILABLE,
        None,
    )

    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: True)
    assert chrome.ensure_running(9222) == (chrome.READY, 9222)

    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: False)
    assert chrome.ensure_running(9222, chrome_bin="/bin/chrome", wait_sec=0.01) == (
        chrome.UNAVAILABLE,
        None,
    )
    assert len(launches) == 2  # cleared, so this launched instead of sitting out the window


def test_a_failed_launch_quiets_further_attempts_for_the_backoff_window(monkeypatch) -> None:
    """A launch that never answered cost its caller the full wait; the lanes that keep asking must
    answer UNAVAILABLE immediately rather than each burning another launch and another wait."""
    import time as _time

    launches = []
    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: False)
    monkeypatch.setattr(chrome, "_LAUNCH_POLL_SEC", 0.0)
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda argv, **kw: launches.append(argv))

    assert chrome.ensure_running(9222, chrome_bin="/bin/chrome", wait_sec=0.01) == (
        chrome.UNAVAILABLE,
        None,
    )
    assert chrome.ensure_running(9222, chrome_bin="/bin/chrome", wait_sec=0.01) == (
        chrome.UNAVAILABLE,
        None,
    )
    assert len(launches) == 1  # the second attempt was quieted, not retried

    # A port that answers is READY regardless — the backoff only quiets launches.
    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: True)
    assert chrome.ensure_running(9222) == (chrome.READY, 9222)

    # Past the window, launching is tried again.
    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: False)
    chrome._last_failed_launch_ts = _time.monotonic() - chrome.FAILED_LAUNCH_BACKOFF_SEC - 1
    assert chrome.ensure_running(9222, chrome_bin="/bin/chrome", wait_sec=0.01) == (
        chrome.UNAVAILABLE,
        None,
    )
    assert len(launches) == 2


def test_the_default_command_pins_the_endpoint_and_its_own_output_dir(xdg_tmp) -> None:
    """The server saves a page snapshot per navigation. Left to itself it writes them into whatever
    directory it started in — a checkout, or wherever the daemon was launched — and those files are
    page content, including the seller's own address off a composer page."""
    from sellee import paths

    argv = default_command("http://127.0.0.1:9222")
    assert argv[:2] == ["npx", "--yes"]
    assert "--cdp-endpoint" in argv and "http://127.0.0.1:9222" in argv
    assert argv[argv.index("--output-dir") + 1] == str(paths.browser_output_dir())
    assert str(paths.state_dir()) in str(paths.browser_output_dir())
    assert "--output-max-size" in argv  # so it evicts its own old files


def test_the_default_command_asks_for_an_exact_version_not_whatever_is_latest() -> None:
    """An unpinned spec leaves each machine on whatever npm resolved first, with nothing between a
    broken upstream release and a seller — and no exact npx cache key to warm."""
    argv = default_command("http://127.0.0.1:9222")
    assert PINNED_MCP_SPEC in argv
    assert "@playwright/mcp" not in argv  # the bare name would float
    assert PINNED_MCP_SPEC.startswith("@playwright/mcp@")
