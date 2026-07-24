"""The harness seam: one internal PassSpec, pure per-provider emitters with round-trip validators.

claude is the live provider (argv + .claude/settings.json + .mcp.json); codex is a stub emitter
(config.toml only, no spawn path) kept honest so the common representation stays common. Each
emitter parses its own output back and asserts it matches the spec — a malformed value is
rejected, never sanitized.
"""

from __future__ import annotations

from selly_agent.harness.model import PassSpec

__all__ = ["PassSpec"]
