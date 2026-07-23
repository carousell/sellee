"""The Telegram provider: everything Bot API-specific behind the channel core.

`transport` is the dumb pipe (the one network module here); `poller` is the long-poll receive
loop and its off/awaiting-bind/bound states; `bind` is the nonce deep-link connect flow;
`commands` holds the "/" menu set and renders the core's control spec into an inline keyboard;
`outbound` supplies the `deliver`/`typing` callables the core outbound policy calls. A second
provider (Slack, iMessage) would be a sibling package implementing the same seams, reusing the
channel core (fastpaths / routing / outbound policy / prompt) unchanged.
"""

from __future__ import annotations
