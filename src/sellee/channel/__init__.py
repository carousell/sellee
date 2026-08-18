"""The channel subsystem, split into a provider-agnostic core and per-provider packages.

Core (this package): `fastpaths` (the deterministic commands' decision + text renders + a
provider-neutral control spec), `routing` (ingest fan-out: the channel.in event + coalesced
channel-pass routing), `outbound` (the notice-drain / typing-pulse *policy* plus the pure-store
fold + escalation-push subscribers), and `prompt` (the channel-pass prompt with its transcript
window). None of these import a provider.

Providers (`channel.telegram`, and future siblings): the transport, the receive loop, the bind
flow, and the `deliver`/`typing` mechanisms. A provider's loop normalizes inbound messages into the
shared event shape, persists them via the store, then calls the core fan-out; it supplies the
core outbound policy with its own send/typing callables. Adding a channel is a new provider
package, not changes to the core.
"""

from __future__ import annotations
