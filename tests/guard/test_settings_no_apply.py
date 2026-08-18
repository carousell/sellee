"""The no-apply-tool invariant, as a test rather than a convention.

The whole settings design rests on one guarantee: the LLM can propose a change but can never apply,
approve, cancel, or undo one — those transitions happen only in deterministic daemon code behind a
human door. Assert it structurally: on every pass tier, the only settings-mutation tool is
propose_setting_change, and no tool anywhere is named for a decision transition.
"""

from __future__ import annotations

import sellee.tools  # noqa: F401  registration
from sellee.tools import (
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_PUBLISH,
    TIER_PASS_REPLY,
)
from sellee.tools.registry import all_specs, tools_for_tier

_ALL_TIERS = (TIER_ATTENDED, TIER_PASS_PUBLISH, TIER_PASS_REPLY, TIER_PASS_CHANNEL)

# Settings-decision names that would betray an apply/approve door leaking onto an LLM tool surface.
# Scoped to setting/change so ordinary domain tools (e.g. cancel_want) are not swept up.
_DECISION_MARKERS = (
    "approve_setting",
    "cancel_setting",
    "undo_setting",
    "apply_setting",
    "decide_setting",
    "approve_change",
    "cancel_change",
    "undo_change",
)

# The one and only settings-mutation tool.
_MUTATION = "propose_setting_change"


def test_only_propose_mutates_settings_on_every_tier() -> None:
    for tier in _ALL_TIERS:
        names = {spec.name for spec in tools_for_tier(tier)}
        settings_tools = {n for n in names if "setting" in n}
        assert settings_tools <= {_MUTATION, "get_settings"}, (tier, settings_tools)
        mutators = settings_tools - {"get_settings"}
        assert mutators <= {_MUTATION}, (tier, mutators)


def test_no_tool_is_named_for_a_decision_transition() -> None:
    for spec in all_specs():
        assert not any(marker in spec.name for marker in _DECISION_MARKERS), spec.name


def test_no_apply_or_approve_tool_exists() -> None:
    names = {spec.name for spec in all_specs()}
    for forbidden in ("apply_setting_change", "approve_setting_change", "undo_setting_change"):
        assert forbidden not in names
