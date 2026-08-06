"""The browser client over a real subprocess: handshake, tool round trip, typed errors, no retry,
and the response framing Playwright MCP actually uses."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from selly_agent import spawn
from selly_agent.browser import chrome
from selly_agent.browser.client import (
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
    monkeypatch.setattr("selly_agent.browser.client.shutil.which", lambda _name: None)
    with pytest.raises(BrowserUnavailable, match="playwright_mcp_cmd"):
        ensure_available(["npx", "--yes", "@playwright/mcp"])


def test_a_present_binary_passes_the_availability_check(monkeypatch) -> None:
    monkeypatch.setattr("selly_agent.browser.client.shutil.which", lambda name: f"/usr/bin/{name}")
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
    assert chrome.is_ready(1, timeout_sec=0.2) is False


def test_the_launch_command_uses_the_agents_own_profile(xdg_tmp) -> None:
    from selly_agent import paths

    argv = chrome.launch_command(9222, chrome_bin="/bin/chrome")
    assert f"--user-data-dir={paths.browser_profile_dir()}" in argv
    assert "--remote-debugging-port=9222" in argv
    assert str(paths.browser_profile_dir()) != str(Path.home() / "Library")


def test_the_launch_command_keeps_a_covered_window_out_of_the_hidden_state(xdg_tmp) -> None:
    """A window the seller has covered with another app otherwise counts as hidden, and a hidden tab
    is one a send has to raise before its keys will land."""
    assert "--disable-backgrounding-occluded-windows" in chrome.launch_command(9222)


def test_a_configured_chrome_path_wins_over_discovery(monkeypatch) -> None:
    """Discovery is a fallback; a seller who named a path meant it."""
    monkeypatch.setattr(chrome, "chrome_candidates", lambda: ["/never/consulted"])
    assert chrome.resolve_binary("/opt/my-chrome") == "/opt/my-chrome"


def test_discovery_prefers_a_candidate_that_exists(tmp_path, monkeypatch) -> None:
    absent, present = tmp_path / "nope" / "chrome", tmp_path / "chrome"
    present.write_text("")
    monkeypatch.setattr(chrome, "chrome_candidates", lambda: [str(absent), str(present)])

    assert chrome.resolve_binary() == str(present)


def test_discovery_with_nothing_installed_still_names_a_path(tmp_path, monkeypatch) -> None:
    """The caller reports this in "Chrome is not installed", so it has to be a path somebody can
    read and check rather than an empty string."""
    monkeypatch.setattr(chrome, "chrome_candidates", lambda: ["/a/chrome", "/b/chrome"])

    assert chrome.resolve_binary() == "/b/chrome"


def test_the_lock_names_match_what_this_platform_leaves_behind() -> None:
    """Clearing the wrong names is worse than clearing none: the launch afterwards hangs."""
    names = chrome.singleton_lock_names()
    assert names == (
        ("lockfile",)
        if os.name == "nt"
        else ("SingletonLock", "SingletonCookie", "SingletonSocket")
    )


def test_stale_singleton_locks_are_cleared(xdg_tmp) -> None:
    """A SIGKILLed Chrome leaves these behind and the next launch hangs on them."""
    from selly_agent import paths

    paths.ensure_data_dirs()
    for name in chrome.singleton_lock_names():
        (paths.browser_profile_dir() / name).write_text("")
    assert sorted(chrome.clear_stale_locks()) == sorted(chrome.singleton_lock_names())
    assert chrome.clear_stale_locks() == []  # idempotent


def test_ensure_running_launches_nothing_when_chrome_already_answers(monkeypatch) -> None:
    """Two Chromes cannot share the profile, so a live port is the end of it."""
    launches = []
    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: True)
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda *a, **kw: launches.append(a))

    assert chrome.ensure_running(9222) == chrome.READY
    assert launches == []


def test_ensure_running_starts_chrome_and_waits_for_the_port(xdg_tmp, monkeypatch) -> None:
    from selly_agent import paths

    paths.ensure_data_dirs()
    (paths.browser_profile_dir() / chrome.singleton_lock_names()[0]).write_text("")
    answers = iter([False, False, True])
    launched = {}

    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: next(answers))
    monkeypatch.setattr(chrome, "_LAUNCH_POLL_SEC", 0.0)
    monkeypatch.setattr(
        chrome.subprocess, "Popen", lambda argv, **kw: launched.update(argv=argv, kw=kw)
    )

    assert chrome.ensure_running(9222, chrome_bin="/bin/chrome") == chrome.LAUNCHED
    assert launched["argv"][0] == "/bin/chrome"
    # Its own session/group: the daemon exiting, or a pass group being killed, must not take the
    # browser with it. Asked of the helper, so the assertion holds on whichever OS runs the suite.
    for key, value in spawn.survives_us_flags().items():
        assert launched["kw"][key] == value
    # The lock only goes once the probe has said nobody is answering.
    assert not (paths.browser_profile_dir() / chrome.singleton_lock_names()[0]).exists()


def test_a_launch_wait_gives_up_as_soon_as_the_daemon_is_stopping(monkeypatch) -> None:
    """The daemon's drain waits for its lanes, so a lane still sitting out the full launch wait is
    a `daemon stop` that looks wedged for twenty seconds. Chrome is detached and comes up anyway."""
    polls = []
    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: polls.append(1) or False)
    monkeypatch.setattr(chrome, "_LAUNCH_POLL_SEC", 0.0)
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda argv, **kw: None)

    state = chrome.ensure_running(
        9222, chrome_bin="/bin/chrome", wait_sec=3600.0, should_stop=lambda: True
    )
    assert state == chrome.UNAVAILABLE
    # The opening readiness probe, then one poll inside the wait — not an hour of them.
    assert len(polls) == 2


def test_ensure_running_reports_unavailable_when_chrome_never_answers(monkeypatch) -> None:
    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: False)
    monkeypatch.setattr(chrome, "_LAUNCH_POLL_SEC", 0.0)
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda argv, **kw: None)

    assert chrome.ensure_running(9222, chrome_bin="/bin/chrome", wait_sec=0.01) == (
        chrome.UNAVAILABLE
    )


def test_ensure_running_reports_unavailable_when_the_binary_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: False)

    def _boom(argv, **kw):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(chrome.subprocess, "Popen", _boom)
    assert chrome.ensure_running(9222, chrome_bin="/nope/chrome") == chrome.UNAVAILABLE


def test_two_concurrent_callers_launch_one_chrome(xdg_tmp, monkeypatch) -> None:
    """Two lanes can want the browser in the same window. Without the launch lock both would see a
    silent port, both would clear the profile locks, and both would launch — two Chromes contending
    one profile. The loser must instead find the port answering and launch nothing."""
    import threading

    from selly_agent import paths

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
    assert sorted(results) == [chrome.LAUNCHED, chrome.READY]


def test_a_live_port_clears_the_failed_launch_backoff(monkeypatch) -> None:
    """A port that answers is evidence of the thing the backoff infers against, so it ends the
    window early: a seller who starts Chrome by hand and later closes it gets a launch, not a
    can't-drive-a-browser refusal for the remaining minutes."""
    launches = []
    monkeypatch.setattr(chrome, "_LAUNCH_POLL_SEC", 0.0)
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda argv, **kw: launches.append(argv))

    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: False)
    assert chrome.ensure_running(9222, chrome_bin="/bin/chrome", wait_sec=0.01) == (
        chrome.UNAVAILABLE
    )

    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: True)
    assert chrome.ensure_running(9222) == chrome.READY

    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: False)
    assert chrome.ensure_running(9222, chrome_bin="/bin/chrome", wait_sec=0.01) == (
        chrome.UNAVAILABLE
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
        chrome.UNAVAILABLE
    )
    assert chrome.ensure_running(9222, chrome_bin="/bin/chrome", wait_sec=0.01) == (
        chrome.UNAVAILABLE
    )
    assert len(launches) == 1  # the second attempt was quieted, not retried

    # A port that answers is READY regardless — the backoff only quiets launches.
    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: True)
    assert chrome.ensure_running(9222) == chrome.READY

    # Past the window, launching is tried again.
    monkeypatch.setattr(chrome, "is_ready", lambda port, **kw: False)
    chrome._last_failed_launch_ts = _time.monotonic() - chrome.FAILED_LAUNCH_BACKOFF_SEC - 1
    assert chrome.ensure_running(9222, chrome_bin="/bin/chrome", wait_sec=0.01) == (
        chrome.UNAVAILABLE
    )
    assert len(launches) == 2


def test_the_default_command_pins_the_endpoint_and_its_own_output_dir(xdg_tmp) -> None:
    """The server saves a page snapshot per navigation. Left to itself it writes them into whatever
    directory it started in — a checkout, or wherever the daemon was launched — and those files are
    page content, including the seller's own address off a composer page."""
    from selly_agent import paths

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
