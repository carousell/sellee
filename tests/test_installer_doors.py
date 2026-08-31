"""The doors the installer needs: settings-set, seller-basics, and marketplace sign-in.

All three are attended-only control routes — the seller reaching their own daemon — and all three
validate exactly as the model-facing path does. What they skip is the approval round-trip, which
exists to gate the model, not the person typing.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest
from tests.conftest import seed_setting

import sellee.tools  # noqa: F401  tool registration
from sellee import settings
from sellee.browser.client import BrowserUnavailable
from sellee.config import Config
from sellee.http_server import HttpServer
from sellee.tools.registry import ToolContext


class FakeBrowser:
    """A browser client that records where it was sent and answers a scripted login state.

    It knows which tab it is on, because the real client can lose that: bringing a tab forward
    selects one before it can check which tab it got, and a select repoints every later call. So
    `evaluate` answers about the current tab rather than unconditionally, and a scripted
    `front_steals_the_tab` reproduces the case where that tab is no longer ours.
    """

    def __init__(self, state="logged_in"):
        self.state = state
        self.visited = []
        self.fronted = []
        self.front_error = None
        self.front_steals_the_tab = False
        self.on_our_tab = True
        self.exclusive_depth = 0

    def exclusive(self):
        client = self

        class _Held:
            def __enter__(self):
                client.exclusive_depth += 1
                return client

            def __exit__(self, *exc):
                client.exclusive_depth -= 1
                return False

        return _Held()

    def navigate(self, url):
        self.visited.append(url)
        self.on_our_tab = True

    def ensure_frontmost(self, url):
        assert self.exclusive_depth > 0, "the tab select must run under the navigate's hold"
        if self.front_error is not None:
            if self.front_steals_the_tab:
                self.on_our_tab = False
            raise self.front_error
        self.fronted.append(url)

    def evaluate(self, function, **kwargs):
        assert self.exclusive_depth > 0, "the probe must run under the same hold as the navigate"
        if not self.on_our_tab:
            # Some page of the seller's, which a market's login artifact reads as signed out.
            return {"state": "logged_out"}
        return {"state": self.state} if self.state else None


@pytest.fixture
def browser():
    return FakeBrowser()


@pytest.fixture
def chrome_up(monkeypatch):
    """Chrome already answering, which is what lets a read probe run at all."""
    from sellee.browser import chrome

    monkeypatch.setattr(chrome, "is_ready", lambda port, **kwargs: True)


@pytest.fixture
def server(bus, store, xdg_tmp, browser):
    def context_factory(session):
        return ToolContext(
            session=session,
            store=store,
            bus=bus,
            config=Config(),
            browser_factory=lambda: browser,
            started_ts=1.0,
        )

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


def _call(server, method, path, *, token="attended-secret", body=None):
    url = f"http://127.0.0.1:{server.port}{path}"
    if method == "GET" and token:
        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}token={urllib.parse.quote(token)}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if method != "GET" and token:
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


# --- settings-set --------------------------------------------------------------------------


def test_setting_a_value_applies_it_immediately(server, store) -> None:
    status, body = _call(
        server, "POST", "/control/settings-set", body={"key": "firmness", "value": "firm"}
    )
    assert status == 200
    assert body["status"] == "applied"
    assert settings.get(store, "firmness") == "firm"


def test_a_json_encoded_list_arrives_as_a_list(server, store) -> None:
    store.set_seller_config_section("basics", {"region": "SG"})
    status, body = _call(
        server,
        "POST",
        "/control/settings-set",
        body={"key": "connected_markets", "value": '["carousell"]'},
    )
    assert status == 200
    assert settings.get(store, "connected_markets") == ["carousell"]
    assert body["rendered"] == "Carousell"


def test_the_prior_value_is_recorded_so_undo_works(server, store, bus) -> None:
    _call(server, "POST", "/control/settings-set", body={"key": "firmness", "value": "firm"})
    status, body = _call(
        server, "POST", "/control/settings-set", body={"key": "firmness", "value": "soft"}
    )
    change_id = body["change_id"]

    result = settings.decide(store, bus, change_id=change_id, decision="undo", decided_via="cli")
    assert result["status"] == "undone"
    assert settings.get(store, "firmness") == "firm"


def test_setting_the_current_value_changes_nothing(server, store) -> None:
    status, body = _call(
        server, "POST", "/control/settings-set", body={"key": "firmness", "value": "balanced"}
    )
    assert status == 200
    assert body["status"] == "unchanged"
    assert store.list_pending_changes() == []


def test_a_value_the_registry_refuses_is_a_400_with_the_reason(server) -> None:
    status, body = _call(
        server, "POST", "/control/settings-set", body={"key": "firmness", "value": "ferocious"}
    )
    assert status == 400
    assert "soft" in body["error"]  # names what is allowed


def test_an_unknown_setting_is_a_400_naming_the_real_ones(server) -> None:
    status, body = _call(
        server, "POST", "/control/settings-set", body={"key": "nonsense", "value": 1}
    )
    assert status == 400
    assert "quiet_hours" in body["error"]


def test_the_seller_state_check_runs_at_this_door_too(server, store) -> None:
    # A US seller cannot be listed on Carousell, which runs no US site. Refused here exactly as
    # it is refused when the model proposes it.
    store.set_seller_config_section("basics", {"region": "US"})
    seed_setting(store, "connected_markets", [])
    status, body = _call(
        server,
        "POST",
        "/control/settings-set",
        body={"key": "connected_markets", "value": ["carousell"]},
    )
    assert status == 400
    assert "US" in body["error"]
    assert settings.get(store, "connected_markets") == []


def test_settings_set_needs_the_attended_token(server) -> None:
    status, _ = _call(
        server,
        "POST",
        "/control/settings-set",
        token=None,
        body={"key": "firmness", "value": "firm"},
    )
    assert status == 401


# --- seller-basics --------------------------------------------------------------------------


def test_basics_are_written_and_upper_cased(server, store) -> None:
    status, body = _call(
        server,
        "POST",
        "/control/seller-basics",
        body={"region": "sg", "currency": "sgd", "timezone": "Asia/Singapore"},
    )
    assert status == 200
    assert body["basics"] == {
        "region": "SG",
        "currency": "SGD",
        "timezone": "Asia/Singapore",
    }
    # Normalized at the write, so the registry's exact-match region lookup finds a site.
    assert store.seller_region() == "SG"


def test_basics_merge_rather_than_replace(server, store) -> None:
    _call(server, "POST", "/control/seller-basics", body={"region": "SG", "currency": "SGD"})
    _call(server, "POST", "/control/seller-basics", body={"timezone": "Asia/Singapore"})
    assert store.get_seller_config_section("basics") == {
        "region": "SG",
        "currency": "SGD",
        "timezone": "Asia/Singapore",
    }


def test_a_malformed_region_is_refused(server, store) -> None:
    status, body = _call(server, "POST", "/control/seller-basics", body={"region": "Singapore"})
    assert status == 400
    assert "two-letter" in body["error"]
    assert store.seller_region() is None


def test_an_unknown_timezone_is_refused(server) -> None:
    status, body = _call(
        server, "POST", "/control/seller-basics", body={"timezone": "Mars/Olympus_Mons"}
    )
    assert status == 400
    assert "timezone" in body["error"]


def test_an_empty_basics_body_says_so(server) -> None:
    status, body = _call(server, "POST", "/control/seller-basics", body={})
    assert status == 400
    assert "at least one" in body["error"]


def test_an_unknown_basics_key_is_refused(server) -> None:
    status, body = _call(server, "POST", "/control/seller-basics", body={"county": "SG"})
    assert status == 400
    assert "county" in body["error"]


# --- marketplace sign-in --------------------------------------------------------------------


def test_connect_market_opens_the_regional_site_and_reports_the_probe(
    server, store, browser
) -> None:
    store.set_seller_config_section("basics", {"region": "SG"})
    status, body = _call(server, "POST", "/control/connect-market", body={"market": "carousell"})
    assert status == 200
    assert body == {
        "market": "carousell",
        "url": "https://www.carousell.sg/",
        "state": "logged_in",
        "raise_window": True,
    }
    assert browser.visited == ["https://www.carousell.sg/"]


def test_connect_market_reports_the_sellers_window_preference(server, store) -> None:
    """The CLI does the OS-level raise, but the setting lives in the daemon's store — so the
    answer rides in the response for the CLI to obey."""
    from tests.conftest import seed_setting

    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "raise_browser", False)
    _status, body = _call(server, "POST", "/control/connect-market", body={"market": "carousell"})
    assert body["raise_window"] is False


def test_connect_market_brings_the_agents_own_tab_forward(server, store, browser) -> None:
    store.set_seller_config_section("basics", {"region": "SG"})
    _call(server, "POST", "/control/connect-market", body={"market": "carousell"})
    assert browser.fronted == ["https://www.carousell.sg/"]


def test_a_tab_that_will_not_come_forward_never_fails_the_sign_in(server, store, browser) -> None:
    from sellee.browser.client import BrowserToolError

    store.set_seller_config_section("basics", {"region": "SG"})
    browser.front_error = BrowserToolError("the tab stayed hidden")
    status, body = _call(server, "POST", "/control/connect-market", body={"market": "carousell"})
    assert status == 200
    assert body["state"] == "logged_in"


def test_a_raise_that_lost_our_tab_reports_our_page_not_whatever_it_landed_on(
    server, store, browser
) -> None:
    """The select that brings a tab forward happens before it can tell which tab it got, so a
    failure can point every later call at the seller's own page — and the probe is a later call.
    Answering about that page would report a signed-in seller as signed out."""
    from sellee.browser.client import BrowserToolError

    store.set_seller_config_section("basics", {"region": "SG"})
    browser.front_error = BrowserToolError("selecting our own tab landed somewhere else")
    browser.front_steals_the_tab = True
    status, body = _call(server, "POST", "/control/connect-market", body={"market": "carousell"})
    assert status == 200
    assert body["state"] == "logged_in"
    # Re-navigated first: the market is back in a tab of ours, which is what the probe answers for.
    assert browser.visited == ["https://www.carousell.sg/"] * 2


def test_login_probes_never_touch_the_tab_order(server, store, browser, chrome_up) -> None:
    """The connect route is the one being asked for a window; a read probe that reordered tabs
    would elbow the seller mid-browse — the anti-focus-stealing guarantee, pinned."""
    from tests.conftest import seed_setting

    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])
    _call(server, "POST", "/control/market-login", body={"market": "carousell"})
    _call(server, "POST", "/control/market-logins", body={})
    assert browser.fronted == []


def test_the_three_login_states_come_back_verbatim(server, store, browser, chrome_up) -> None:
    store.set_seller_config_section("basics", {"region": "SG"})
    for state in ("logged_in", "logged_out", "unknown"):
        browser.state = state
        _status, body = _call(server, "POST", "/control/market-login", body={"market": "carousell"})
        assert body["state"] == state


def test_an_unreadable_probe_answers_unknown_never_logged_out(
    server, store, browser, chrome_up
) -> None:
    # A false logged_out tells a signed-in seller to re-authenticate and stops their market.
    store.set_seller_config_section("basics", {"region": "SG"})
    browser.state = None
    _status, body = _call(server, "POST", "/control/market-login", body={"market": "carousell"})
    assert body["state"] == "unknown"


def test_a_market_we_cannot_drive_is_refused_with_the_list_we_can(server, store) -> None:
    store.set_seller_config_section("basics", {"region": "SG"})
    status, body = _call(server, "POST", "/control/connect-market", body={"market": "ebay"})
    assert status == 400
    assert "carousell" in body["error"]


def test_a_market_with_no_site_in_the_sellers_region_says_so(server, store) -> None:
    store.set_seller_config_section("basics", {"region": "US"})
    status, body = _call(server, "POST", "/control/connect-market", body={"market": "carousell"})
    assert status == 503
    assert "US" in body["detail"]


def test_a_browser_that_will_not_start_is_a_503_with_the_hint(bus, store, xdg_tmp) -> None:
    def context_factory(session):
        def refuse():
            raise BrowserUnavailable("start Chrome with: /Applications/…")

        return ToolContext(
            session=session,
            store=store,
            bus=bus,
            config=Config(),
            browser_factory=refuse,
            started_ts=1.0,
        )

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
        store.set_seller_config_section("basics", {"region": "SG"})
        status, body = _call(srv, "POST", "/control/connect-market", body={"market": "carousell"})
    finally:
        srv.stop()
    assert status == 503
    assert "start Chrome" in body["detail"]


def test_market_login_needs_the_attended_token(server) -> None:
    status, _ = _call(
        server, "POST", "/control/market-login", body={"market": "carousell"}, token=None
    )
    assert status == 401


# --- the healthcheck's market read -----------------------------------------------------------


def test_market_logins_reports_the_enabled_set_and_probes_when_chrome_is_up(
    server, store, browser, monkeypatch
) -> None:
    from tests.conftest import seed_setting

    from sellee.browser import chrome

    monkeypatch.setattr(chrome, "is_ready", lambda port, **kwargs: True)
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])

    _status, body = _call(server, "POST", "/control/market-logins", body={})

    assert body["enabled"] == ["carousell"]
    assert body["blocked"] == ""
    assert body["markets"] == [{"market": "carousell", "state": "logged_in"}]


def test_market_logins_never_opens_a_window_just_to_answer(
    server, store, browser, monkeypatch
) -> None:
    # A status read that starts Chrome is not a status read: with the port silent, the enabled
    # list still comes back and no probe is attempted.
    from tests.conftest import seed_setting

    from sellee.browser import chrome

    monkeypatch.setattr(chrome, "is_ready", lambda port, **kwargs: False)
    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])

    _status, body = _call(server, "POST", "/control/market-logins", body={})

    assert body["enabled"] == ["carousell"]
    assert "Chrome isn't running" in body["blocked"]
    assert body["markets"] == []
    assert browser.visited == []


def test_market_logins_names_the_pass_as_the_reason_not_a_closed_chrome(
    server, store, browser, chrome_up
) -> None:
    # Both reasons stop the probe, but they must not be conflated: a report claiming "Chrome
    # isn't running" while Chrome is visibly mid-publish teaches the reader to distrust it.
    from tests.conftest import seed_setting

    store.set_seller_config_section("basics", {"region": "SG"})
    seed_setting(store, "connected_markets", ["carousell"])
    store.enqueue_pass("publish", {"item_id": "itm_1", "market": "carousell"})

    _status, body = _call(server, "POST", "/control/market-logins", body={})

    assert "a pass is using the browser" in body["blocked"]
    assert body["markets"] == []
    assert browser.visited == []


def test_market_logins_filters_to_what_is_still_publishable(
    server, store, browser, monkeypatch
) -> None:
    # Carousell runs no US site, so a stored id stops counting when the region moves.
    from tests.conftest import seed_setting

    from sellee.browser import chrome

    monkeypatch.setattr(chrome, "is_ready", lambda port, **kwargs: True)
    store.set_seller_config_section("basics", {"region": "US"})
    seed_setting(store, "connected_markets", ["carousell"])

    _status, body = _call(server, "POST", "/control/market-logins", body={})

    assert body["enabled"] == []


def test_a_login_read_never_opens_a_window_when_chrome_is_closed(
    server, store, browser, monkeypatch
) -> None:
    # Probing acquires the browser, and acquiring starts Chrome. A read must not do that.
    # The port has to be silenced explicitly: is_ready does a real loopback GET, so left alone
    # this passes or fails on whether the machine running the suite has a Chrome of its own.
    from sellee.browser import chrome

    monkeypatch.setattr(chrome, "is_ready", lambda port, **kwargs: False)
    store.set_seller_config_section("basics", {"region": "SG"})
    _status, body = _call(server, "POST", "/control/market-login", body={"market": "carousell"})
    assert body["state"] == "unknown"
    assert "Chrome isn't running" in body["detail"]
    assert browser.visited == []


def test_a_login_read_yields_to_a_pass_already_driving_the_browser(
    server, store, browser, chrome_up
) -> None:
    # One tab, one driver: navigating it mid-pass would move the page out from under the pass.
    store.set_seller_config_section("basics", {"region": "SG"})
    store.enqueue_pass("publish", {"item_id": "itm_1", "market": "carousell"})

    _status, body = _call(server, "POST", "/control/market-login", body={"market": "carousell"})

    assert body["state"] == "unknown"
    assert "a pass is using the browser" in body["detail"]
    assert browser.visited == []


def test_connect_market_still_opens_a_window_because_that_is_what_it_is_for(
    server, store, browser
) -> None:
    # The read routes decline when Chrome is closed; the connect route is the one that may start
    # it, because being asked to open a marketplace is being asked for a window.
    store.set_seller_config_section("basics", {"region": "SG"})
    _status, body = _call(server, "POST", "/control/connect-market", body={"market": "carousell"})
    assert body["state"] == "logged_in"
    assert browser.visited == ["https://www.carousell.sg/"]


def test_connect_market_waits_its_turn_behind_a_pass(server, store, browser) -> None:
    # Chrome closed is the connect route's job to fix; a pass mid-drive is not. Navigating the
    # shared tab now would pull the page out from under a half-filled composer.
    store.set_seller_config_section("basics", {"region": "SG"})
    store.enqueue_pass("publish", {"item_id": "itm_1", "market": "carousell"})

    status, body = _call(server, "POST", "/control/connect-market", body={"market": "carousell"})

    assert status == 409
    assert "a pass is using the browser" in body["detail"]
    assert browser.visited == []


def test_a_structurally_invalid_timezone_is_refused_not_shrugged_at(server) -> None:
    status, body = _call(
        server, "POST", "/control/seller-basics", body={"timezone": "../../etc/passwd"}
    )
    assert status == 400
    assert "not a valid timezone" in body["error"]


def test_a_country_the_rail_does_not_serve_is_refused_at_the_door(server, store) -> None:
    status, body = _call(server, "POST", "/control/seller-basics", body={"region": "MY"})
    assert status == 400
    assert "MY isn't a country sellee works in yet" in body["error"]
    assert "SG, US" in body["error"]
    assert store.seller_region() is None


def test_the_model_is_held_to_the_same_region_rule_as_the_installer(make_ctx) -> None:
    # One validator behind both writers, so the LLM cannot record what the door refuses.
    from sellee.tools.registry import ToolError, dispatch

    ctx = make_ctx("attended")
    with pytest.raises(ToolError) as caught:
        dispatch("update_seller_config", {"basics": {"region": "MY"}}, ctx)
    assert "isn't a country sellee works in yet" in str(caught.value)


def test_the_model_updating_one_basics_key_keeps_the_rest(make_ctx, store) -> None:
    # Merged like the door writes it. validate_basics accepts partial updates, so a currency
    # tweak that replaced the whole section would silently drop the region the installer
    # recorded — after which nothing can publish and nothing says why.
    from sellee.tools.registry import dispatch

    store.set_seller_config_section(
        "basics", {"region": "SG", "currency": "SGD", "timezone": "Asia/Singapore"}
    )
    dispatch("update_seller_config", {"basics": {"currency": "USD"}}, make_ctx("attended"))

    assert store.get_seller_config_section("basics") == {
        "region": "SG",
        "currency": "USD",
        "timezone": "Asia/Singapore",
    }


# --- the control client, against the real routes ---------------------------------------------


def test_the_control_client_speaks_the_server_dialect_end_to_end(server, store) -> None:
    # Every CLI verb funnels through control.post/get, and every CLI test stubs them — so this
    # is the one place a drift between client and server (the token in the wrong place, an
    # error body dropped) shows up before a live daemon does.
    from sellee import control

    status, body = control.post(
        server.port, "attended-secret", "/control/seller-basics", {"region": "SG"}
    )
    assert (status, body["basics"]["region"]) == (200, "SG")

    status, body = control.get(server.port, "attended-secret", "/control/seller-basics")
    assert (status, body["basics"]["region"]) == (200, "SG")


def test_the_control_client_returns_a_refusal_rather_than_calling_the_daemon_down(server) -> None:
    # A 4xx is the daemon *answering*. Reporting it as unreachable sends whoever reads the
    # message off to restart a daemon that is running fine.
    from sellee import control

    status, body = control.post(
        server.port, "attended-secret", "/control/seller-basics", {"region": "ZZ"}
    )
    assert status == 400
    assert "isn't a country" in body["error"]

    status, _body = control.get(server.port, "wrong-token", "/control/seller-basics")
    assert status == 401


def test_the_control_client_reserves_unreachable_for_nothing_answering() -> None:
    import socket

    from sellee import control

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]  # released on close; nothing will be listening there

    with pytest.raises(control.DaemonUnreachable):
        control.get(port, "tok", "/control/seller-basics", timeout=2)
