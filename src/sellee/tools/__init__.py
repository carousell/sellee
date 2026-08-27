"""The typed MCP tool surface.

Importing this package registers every tool (each module calls register() at import). The
server imports it once at startup; tests import it to exercise the registry. Keep this import
list complete — an unimported tool module is a tool that silently doesn't exist.
"""

from __future__ import annotations

from sellee.tools import (  # noqa: F401  imported for registration
    browser,
    buyer,
    checkout,
    control,
    escalations,
    listing,
    messaging,
    negotiate,
    photos,
    publish,
    qa,
    reads,
    reply,
    scam,
    seller,
    settings,
    survey,
    threads,
    verify,
    wants,
    writes,
)
from sellee.tools.registry import (  # noqa: F401  re-exported as the package's public surface
    TIER_ATTENDED,
    TIER_PASS_CHANNEL,
    TIER_PASS_PUBLISH,
    TIER_PASS_REPLY,
    Session,
    ToolContext,
    ToolError,
    ToolSpec,
    UnknownTool,
    all_specs,
    dispatch,
    tools_for_tier,
)
