"""The Sellee rail — wrapped behind our typed tools; it never appears on the LLM surface.

client.py speaks JSON-RPC to the Sellee MCP server over HTTP and does the live listing
verify; provision.py obtains the guest API key. Both import network stdlib and are listed in the
stdlib-only guard's network allowlist.
"""

from __future__ import annotations

from sellee import __version__

# Every request to Sellee's API carries this. Cloudflare fronts the API and 403s urllib's
# default User-Agent, and the version lets its analytics show which releases still call a host.
USER_AGENT = f"SELLEE-rail/{__version__}"
