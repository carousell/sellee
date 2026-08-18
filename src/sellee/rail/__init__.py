"""The carousell.ai rail — wrapped behind our typed tools; it never appears on the LLM surface.

client.py speaks JSON-RPC to the carousell.ai MCP server over HTTP and does the live listing
verify; provision.py obtains the guest API key. Both import network stdlib and are listed in the
stdlib-only guard's network allowlist.
"""

from __future__ import annotations
