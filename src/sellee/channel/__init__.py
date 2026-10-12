"""The channel subsystem, split into a provider-agnostic core and per-provider packages.

Core (this package): `fastpaths` (the deterministic commands' decision + text renders + a
provider-neutral control spec), `routing` (ingest fan-out: the channel.in event + coalesced
channel-pass routing + marking a message seen), `acks` (the two arrivals a typing indicator would
lie about), `presence` (the indicator's cadence, and the threshold notice for a wait it cannot
carry),
`outbound` (the notice-drain policy and the typing gate, plus the pure-store fold + escalation-push
subscribers), and `prompt` (the channel-pass prompt with its transcript window). None of these
import a provider.

Providers (`channel.telegram`, and future siblings): the transport, the receive loop, the bind
flow, and the `deliver`/`typing` mechanisms. A provider's loop normalizes inbound messages into the
shared event shape, persists them via the store, then calls the core fan-out; it supplies the
core outbound policy with its own send/typing callables. Adding a channel is a new provider
package, not changes to the core.
"""

from __future__ import annotations

# What a seller calls each provider. Adapter ids are internal; anything printed at a terminal or
# sent to a chat uses these.
_DISPLAY_NAMES = {"telegram": "Telegram", "discord": "Discord"}


def display_name(adapter: str) -> str:
    """The human name for an adapter id, or the id itself (fail-open) for an unknown one."""
    return _DISPLAY_NAMES.get(adapter) or adapter
