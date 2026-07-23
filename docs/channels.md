# The channel subsystem

The channel is how the agent talks to the seller asynchronously — buyer
escalations land on their phone, and they can steer the agent back. It is
**optional**: the daemon runs fully without one, and the needs-me queue (things
awaiting the seller) still works — surfaced at an attended session's catch-up
instead of pushed. Telegram is the only provider today; the design keeps a second
one (Slack, iMessage) a sibling package rather than a rewrite.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for where this sits in the whole.

## Core vs. provider

The package splits the *what* (provider-agnostic) from the *how* (per-provider);
a guard test enforces that the core imports no provider.

- **Core** (`channel/`):
  - `fastpaths` — the deterministic commands (`/pause` `/resume` `/status`
    `/catchup` `/selly`): decide, render reply text, and emit a provider-neutral
    **control spec** (a list of `(label, token)` buttons).
  - `routing` — after a batch is ingested: the `channel.in` event and coalesced
    routing of pending free text to a channel pass.
  - `outbound` — the delivery *policy* (notice drain, typing pulse) plus two
    pure-store bus subscribers (fold a channel pass's rows; push each escalation
    as a notice).
  - `prompt` — the channel pass's prompt with its capped transcript window.
- **Provider** (`channel/telegram/`):
  - `transport` — the Bot API client (the one network module; allowlisted) and
    the pure `_normalize` (update → event) with its helpers.
  - `poller` — the long-poll receive loop and its states.
  - `bind` — the nonce deep-link connect flow.
  - `commands` — the "/" menu set and rendering a control spec into an inline
    keyboard.
  - `outbound` — the `deliver`/`typing` callables the core policy calls.
  - `provider` — `start` / `is_configured` / the handle's `shutdown`.

## Providers run only when connected

A `ChannelManager` (core) owns which providers are *running*:

- `register(name)` starts one (idempotent); `deregister(name)` stops it;
  `shutdown_all()` at daemon stop.
- The daemon registers already-configured providers at boot; the `connect` route
  registers one at runtime.
- So a daemon with no channel set up starts **no channel thread at all** — "off"
  is the absence of a thread, not a thread doing nothing.

Each provider exposes `is_configured()` (for Telegram, "a bot token exists"),
`start(deps) -> handle` (spins its receive loop and registers its delivery lanes,
so those exist only while it runs), and `handle.shutdown()` (stops the loop and
removes those lanes).

## Bind (Telegram)

`selly-agent connect telegram` pipes the BotFather token on stdin (never argv) to
the running daemon, which:

1. validates it (`getMe`), writes it to a 0600 file, and mints a one-time
   **nonce**;
2. returns `t.me/<bot>?start=<nonce>` and starts the provider;
3. binds the first chat whose `/start` carries that nonce — and no other.

Because authorization is nonce possession, not first contact, the hijack race a
first-contact capture would have can't happen, and an interrupted bind resumes
after a restart (the nonce is durable). The token never appears in an event or a
log.

## The poller's three states

One thread owns *all* Bot API traffic, so "an unbound channel consumes nothing"
is a property of that single consumer. State is derived from durable rows each
tick, always failing toward the less-capable one:

- **off** — no token (or a token with no nonce and no chat): zero API calls.
- **awaiting-bind** — token + nonce, no chat: only a `/start` matching the nonce
  binds; everything else is consumed and discarded.
- **bound** — a chat is bound: only that chat's updates are ingested.

## Durable inbox

- Each inbound batch is persisted and the cursor advanced in **one transaction**
  (persist-then-ack), with media downloaded first; re-delivery after a crash is
  deduped by the update id.
- Fast paths are answered by daemon code immediately (a button tap is acked
  first, then the reply).
- Everything else — free text, photos — stays pending and routes to a channel
  pass.

## The needs-me queue

Two durable tables in `selly.db`, so nothing the seller must eventually see lives
only behind the cursor or in the prunable event store:

- open **escalations** (a decision the agent couldn't make),
- queued **notices** (messages/updates for the seller).

Delivery follows one rule — never pretend to push:

- **bound** → the drain lane sends notices FIFO and stamps them delivered;
- **unbound** → `get_catchup` is the delivery path (queue-and-catchup);
- each new escalation is pushed as a notice, with catchup as the crash backstop.

## The channel pass

A phone conversation is state, not a running LLM — so free text spawns a short
`channel` pass that sweeps everything pending and exits.

- Runs full-scope on the provisional `pass:channel` tier (the counterpart is the
  trusted seller, not a buyer).
- Its prompt carries a **recent-transcript window** — inbox rows interleaved with
  the agent's own notices, capped — so a follow-up like "yes, do that" resolves,
  kept separate from the messages to handle now.
- **Coalesced**: at most one channel pass is queued or running; a batch arriving
  mid-pass waits for the next.
- On end: handled on success; on failure the rows are folded failed and one
  notice is queued — never auto-refired.

## Pause

A paused daemon runs but acts on nothing:

- the pass lane claims nothing,
- send-capable tools refuse,
- the babysitter kills a running pass within ~1s,
- the poller and fast paths stay live, so `/resume` is heard.

A missing control row reads as *not paused* (fail toward not-paused).

## Adding a provider

A second channel is a sibling package under `channel/`, not a change to the core.
It brings its own receive mechanism (long-poll, a webhook route, or a socket) and,
once it normalizes inbound messages into the shared event shape
`{event_id, kind, text, payload, src_ts}`, reuses the core unchanged:

- **Reuses**: the store (durable inbox, notices, pass routing, pause),
  `fastpaths`, `routing`, the `outbound` policy, and `prompt`.
- **Supplies**: the transport, event normalization, the bind flow, the
  `deliver`/`typing` mechanisms, and rendering the control spec into its native
  widget — all behind `start` / `shutdown` / `is_configured`.

The receive model is deliberately *not* abstracted — long-poll vs. webhook vs.
socket differ too much to anticipate — so the seam is simply "the provider's loop
calls the core after it has ingested a batch."
