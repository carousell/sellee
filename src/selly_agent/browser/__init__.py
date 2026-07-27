"""The browser layer: the daemon's own Playwright MCP client and the per-market adapters.

Generic machinery lives here — the client, the inbox reader, the reconcile, the reply sink — and
depends only on the market-adapter protocol. Everything a marketplace does differently (which URL
its inbox is at, how to read a chat's tail, where its composer is) lives in one module under
`markets/`, so adding a marketplace is a new file plus a registry entry rather than edits threaded
through the layer.
"""

from __future__ import annotations

from selly_agent.browser.client import (
    BrowserClient,
    BrowserError,
    BrowserToolError,
    BrowserTransportError,
    BrowserUnavailable,
)

__all__ = [
    "BrowserClient",
    "BrowserError",
    "BrowserToolError",
    "BrowserTransportError",
    "BrowserUnavailable",
]
