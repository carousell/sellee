# The channel subsystem

The channel is how the agent talks to the seller asynchronously — buyer
escalations land on their phone, and they can steer the agent back. It is
**optional**: the daemon runs fully without one, and the needs-me queue (things
awaiting the seller) still works — surfaced at an attended session's catch-up
instead of pushed. Telegram and Discord are the providers today; the design keeps
additional ones (Slack, iMessage) as sibling packages rather than rewrites.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for where this sits in the whole.

## Core vs. provider

The package splits the *what* (provider-agnostic) from the *how* (per-provider);
a guard test enforces that the core imports no provider.

- **Core** (`channel/`):
  - `fastpaths` — the deterministic commands (`/pause` `/resume` `/status`
    `/catchup` `/sellee` `/connect` `/watch`): decide, render reply text, and emit a
    provider-neutral **control spec** (a list of `(label, token)` buttons). `/watch`
    and its control-row button flip the `watch_browser` setting through the settings
    ledger — the tap is the consent, so it applies rather than proposing, and the
    refreshed row (whose label now offers the opposite) is the way back.
  - `routing` — after a batch is ingested: the `channel.in` event and coalesced
    routing of pending free text to a channel pass.
  - `outbound` — the delivery *policy* (notice drain, typing pulse), the
    settled-pass inbox fold (a scheduler lane off durable rows), and the
    escalation-push bus subscriber.
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

`sellee connect telegram` reads the BotFather token — never from argv, so it
stays out of `ps`/shell history — and sends it to the running daemon, which:

1. validates it (`getMe`), writes it to a 0600 file, and mints a one-time
   **nonce**;
2. returns `t.me/<bot>?start=<nonce>` and starts the provider;
3. binds the first chat whose `/start` carries that nonce — and no other.

Run interactively (stdin is a TTY), it prints short BotFather guidance and prompts
for the token with `getpass` (not echoed); run with a pipe (scripted / installer),
it reads one line of stdin with no prompt. Either way it then prints the deep link
as a terminal QR (colorless half-blocks in the terminal's own colors — correct
polarity on a dark theme, inverted on a light one, and most scanners read both)
above the link itself, with phone-oriented wording — **scan or open it on the phone that has Telegram**, not
"tap it" (the operator is often at a desktop) — and polls until the chat binds.
The link is the fallback when the terminal render won't scan; rendering is local
by design (an online QR service would ship the single-use nonce off the machine).
The interactive wait is longer (300s vs 120s piped) to cover relaying the link to
a phone.

Because authorization is nonce possession, not first contact, the hijack race a
first-contact capture would have can't happen, and an interrupted bind resumes
after a restart (the nonce is durable). The token never appears in an event or a
log.

The nonce is single-use and expires **15 minutes** after arming
(`store.BIND_NONCE_TTL_SEC`): the connect flow puts the link in a chat history, and an
abandoned bind would otherwise leave the channel adoptable forever. Past the deadline
the provider reads `off` and clears the nonce.

On bind the daemon queues a deterministic welcome as ordinary notices (drain-
delivered, retried, catchup-backstopped — never a fire-and-forget send), stamping
`welcomed_at` in the same transaction so the same bot never re-greets. A seller
with nothing listed yet also gets the **first-listing CTA** — "send a photo of
something you want to sell" — with an inline *Skip for now* button; a seller with
real items never sees it.

## Bind (Discord)

`sellee connect discord` reads the Discord bot token — never from argv, so it stays
out of `ps`/shell history — and sends it to the running daemon, which:

1. validates it against the Discord API and reads the bot's application ID from
   `GET /oauth2/applications/@me` (the seller never has to find it), writes the token to
   a 0600 file, and mints a one-time **nonce**;
2. returns a zero-permission OAuth authorize link (`scope=bot&permissions=0` — the bot
   only ever DMs, so it needs no guild permission grant at all) and starts the provider;
3. binds the DM from the first user who sends the nonce code — and no other.

The flow is deliberately two-step: unlike Telegram's one-tap deep link, the seller
invites the bot to their server first (via OAuth), then sends the nonce in a DM to
establish the binding. This decouples bot installation from session binding and makes
the nonce flow harder to race. Arming goes through the shared `store.arm_bind`, so the nonce has
the same 15-minute deadline as Telegram's.

The provider uses Discord's Gateway connection (WebSocket) instead of long-poll,
connecting once and streaming all events. The gateway intent is scoped to **DIRECT_MESSAGES**
only — it receives only DMs and ignores all server traffic — keeping the connection
minimal. Reading DM text needs no privileged grant: Discord exempts DM content from the
`MESSAGE_CONTENT` intent, which otherwise gates a bot's access to message text and takes
per-server approval.

A dropped connection reconnects on a 5s→60s backoff. The exception is a close code that
will never resolve itself — a revoked token, a refused intent — where reconnecting would
re-authenticate into the same refusal every minute forever. There the gateway goes quiet
for that token, queues one notice explaining why (it surfaces in the needs-me queue, since
the channel that would normally carry it is what is down), and comes back on its own as
soon as `connect discord` writes a different token. No daemon restart needed.

On bind the daemon queues a deterministic welcome as ordinary notices (drain-
delivered, retried, catchup-backstopped — never a fire-and-forget send), stamping
`welcomed_at` in the same transaction so the same bot never re-greets. A seller
with nothing listed yet also gets the **first-listing CTA** — "send a photo of
something you want to sell" — with an inline *Skip for now* button; a seller with
real items never sees it.

## One channel at a time

The `channel` row is a singleton: exactly one provider is bound at any moment,
and connecting the other **replaces** the binding (`store.arm_bind` clears the
sibling's chat, cursor, and welcome stamp).

That singleton is why **`setup` offers the channel as one pick-one menu** rather
than a yes/no per provider: a sequential offer let the first answer decide the
second, so accepting Telegram made Discord read as unavailable and a seller who
had not heard of Discord never learned it existed. The menu shows both, and an
empty answer picks none — as does `--yes` or a pipe, since binding takes a
credential and a phone the absent seller has to supply.

`GET /control/channel-status` answers for whichever provider the row names, and
its `adapter` field says which. Anything that reports a connection to a seller
reads that field — `bound` alone cannot tell a Telegram binding from a Discord
one, and "Discord: already connected" on a Telegram-only install claims a channel
they never set up. So `setup` names the holder instead of re-offering,
`connect <provider> --status` reports only that provider, and the closing "open
\<app\> and send a photo" points at the app actually bound.

## The Telegram poller's three states

One thread owns *all* Bot API traffic, so "an unbound channel consumes nothing"
is a property of that single consumer. State is derived from durable rows each
tick, always failing toward the less-capable one:

- **off** — no token (or a token with no live nonce and no chat): zero API calls.
  An expired nonce lands here and is cleared on the tick.
- **awaiting-bind** — token + an unexpired nonce, no chat: only a `/start`
  matching the nonce binds; everything else is consumed and discarded. The
  deadline is re-read before binding, since a long poll can straddle it.
- **bound** — a chat is bound: only that chat's updates are ingested.

Discord's gateway derives the same three states from the same rows, re-reading
them on every reconnect and on every inbound message (a bind can complete
mid-session). The difference is what "off" costs: a poller makes no API call,
while the gateway holds no WebSocket open at all.

## Durable inbox

- Each inbound batch is persisted and the cursor advanced in **one transaction**
  (persist-then-ack), with media downloaded first; re-delivery after a crash is
  deduped by the update id.
- Fast paths are answered by daemon code immediately (a button tap is acked
  first, then the reply).
- Everything else — free text, photos — stays pending and routes to a channel
  pass.

## The needs-me queue

Two durable tables in `sellee.db`, so nothing the seller must eventually see lives
only behind the cursor or in the prunable event store:

- open **escalations** (a decision the agent couldn't make),
- queued **notices** (messages/updates for the seller).

Delivery follows one rule — never pretend to push:

- **bound** → the drain lane sends notices FIFO and stamps them delivered;
- **unbound** → `get_catchup` is the delivery path (queue-and-catchup);
- each new escalation is pushed as a notice, with catchup as the crash backstop.

One proactive lane feeds this queue: the **first-listing nudge** — a seller bound
for a day who never listed and never tapped Skip gets one nudge, ever (the
notice's own `ref` row is the once-guard, so restarts can't re-fire it). It is
the first `holdable` notice: the drain defers it through quiet hours.

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
- On settle: handled on success; on failure the rows are folded failed and one
  notice is queued — never auto-refired. The fold is a scheduler lane deriving
  from durable rows (a settled pass that still has claimed rows), not a
  `pass.end` subscriber — so any crash shape, including a pass failed by the
  stale-running sweep, still folds and notifies.

## Pause

A paused daemon runs but acts on nothing:

- the pass lane claims nothing,
- send-capable tools refuse,
- the babysitter kills a running pass within ~1s,
- the poller and fast paths stay live, so `/resume` is heard.

A missing control row reads as *not paused* (fail toward not-paused).

## Adding a provider

Each new channel provider is a sibling package under `channel/`, not a change to the core.
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
