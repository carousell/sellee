"""The selector-cache tools and the read-only probe: what a healing pass can and cannot do."""

from __future__ import annotations

import json
import re

import pytest

from selly_agent.browser.client import BrowserUnavailable
from selly_agent.tools import tools_for_tier
from selly_agent.tools.registry import ToolError, UnknownTool, dispatch

_RECORD = {
    "market": "carousell",
    "flow": "reply",
    "step": "message_box",
    "strategy": "css",
    "query": "div.composer",
    "page_url_pattern": "/inbox/",
    "action_kind": "type",
}


class ProbeClient:
    def __init__(self, *, matches=1, url="https://www.carousell.sg/inbox/9/", fail=False):
        self.matches = matches
        self.url = url
        self.fail = fail
        self.evaluated: list = []

    def evaluate(self, function, **kwargs):
        if self.fail:
            raise BrowserUnavailable("npx not found")
        self.evaluated.append(function)
        found = re.search(r"querySelectorAll\((\".*?\")\)", function)
        assert found, function
        return {"matches": self.matches, "url": self.url, "target": json.loads(found.group(1))}


# --- the cache tools ----------------------------------------------------------------------------


def test_record_then_read_round_trips(make_ctx) -> None:
    ctx = make_ctx("attended")
    assert dispatch("ui_cache_record", dict(_RECORD), ctx)["recorded"] is True
    got = dispatch(
        "ui_cache_get", {"market": "carousell", "flow": "reply", "step": "message_box"}, ctx
    )
    assert got["hit"] is True and got["stale"] is False
    assert got["selector"]["query"] == "div.composer"


def test_recording_without_a_page_guard_is_refused(make_ctx) -> None:
    """A selector with no page guard would be resolved against whatever page happened to be open,
    so it could never be trusted — recording one would be recording something unusable."""
    ctx = make_ctx("attended")
    with pytest.raises(ToolError, match="page_url_pattern"):
        dispatch("ui_cache_record", dict(_RECORD, page_url_pattern=""), ctx)


# --- the probe ----------------------------------------------------------------------------------


def test_the_probe_reports_a_single_match_as_usable(make_ctx) -> None:
    client = ProbeClient(matches=1)
    ctx = make_ctx("attended", browser_factory=lambda: client)
    res = dispatch("probe_selector", {"market": "carousell", "selector": "textarea"}, ctx)
    assert res["usable"] is True and res["matches"] == 1
    assert res["url"].endswith("/inbox/9/")


@pytest.mark.parametrize("matches", [0, 2, 7])
def test_anything_but_one_match_is_unusable(make_ctx, matches) -> None:
    """None means it is not there; several means acting on it would be a guess."""
    ctx = make_ctx("attended", browser_factory=lambda: ProbeClient(matches=matches))
    res = dispatch("probe_selector", {"market": "carousell", "selector": "div"}, ctx)
    assert res["usable"] is False


def test_the_probe_is_read_only(make_ctx) -> None:
    client = ProbeClient()
    ctx = make_ctx("attended", browser_factory=lambda: client)
    dispatch("probe_selector", {"market": "carousell", "selector": "textarea"}, ctx)
    sent = "\n".join(client.evaluated)
    for mutation in (".value", "click()", "dispatchEvent", "innerHTML", "submit("):
        assert mutation not in sent


def test_an_unactionable_strategy_is_refused_with_a_reason(make_ctx) -> None:
    ctx = make_ctx("attended", browser_factory=lambda: ProbeClient())
    with pytest.raises(ToolError, match="css, aria, or role"):
        dispatch(
            "probe_selector",
            {"market": "carousell", "selector": "Send", "strategy": "text"},
            ctx,
        )


def test_a_market_with_no_adapter_is_refused(make_ctx) -> None:
    ctx = make_ctx("attended", browser_factory=lambda: ProbeClient())
    with pytest.raises(ToolError, match="adapter"):
        dispatch("probe_selector", {"market": "ebay", "selector": "textarea"}, ctx)


def test_no_browser_surfaces_as_a_tool_error_not_a_crash(make_ctx) -> None:
    def factory():
        raise BrowserUnavailable("npx not found")

    ctx = make_ctx("attended", browser_factory=factory)
    with pytest.raises(ToolError, match="npx not found"):
        dispatch("probe_selector", {"market": "carousell", "selector": "textarea"}, ctx)


def test_a_session_with_no_browser_at_all_says_so(make_ctx) -> None:
    ctx = make_ctx("attended")  # no browser_factory
    with pytest.raises(ToolError, match="not available"):
        dispatch("probe_selector", {"market": "carousell", "selector": "textarea"}, ctx)


# --- tiering ------------------------------------------------------------------------------------


def test_the_healing_surface_is_out_of_the_reply_tier(make_ctx) -> None:
    """A reply pass processes untrusted buyer text. A buyer who could get a selector recorded would
    choose where the agent's next reply gets typed, so there a miss stays a fail-closed send."""
    reply_tools = {spec.name for spec in tools_for_tier("pass:reply")}
    assert not reply_tools & {
        "ui_cache_get",
        "ui_cache_record",
        "ui_cache_invalidate",
        "probe_selector",
    }
    ctx = make_ctx("pass:reply", browser_factory=lambda: ProbeClient())
    with pytest.raises(UnknownTool):
        dispatch("ui_cache_record", dict(_RECORD), ctx)


def test_the_browser_driving_pass_can_heal(make_ctx) -> None:
    publish_tools = {spec.name for spec in tools_for_tier("pass:publish")}
    assert {"ui_cache_get", "ui_cache_record", "probe_selector"} <= publish_tools
