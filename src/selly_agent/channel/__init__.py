"""The channel subsystem: the Telegram transport, the poller thread, and the fast-path commands.

One thread owns all Bot API traffic (the poller), so "an unbound channel consumes nothing" is a
property of that single consumer rather than a convention spread across callers. The transport
(`telegram.py`) is the only network module here and is added to the stdlib-only guard's network
allowlist deliberately.
"""

from __future__ import annotations
