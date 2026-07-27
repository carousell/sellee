# Architecture

A high-level map of the repository. It describes the shape of the program and
where responsibilities live; read the modules themselves for detail. As more
subsystems land, this page becomes the index that links out to their docs.

## The one-process model

selly-agent is a single long-running Python process, kept alive by the OS
(launchd on macOS). It is stdlib-only at runtime: the user's own `python3` is
the only runtime dependency, enforced by a guard test over `src/` imports.
Concurrency is a few threads sharing SQLite state.

Everything is reachable from one front door: `bin/selly-agent` resolves the
package and dispatches argv via `cli.py` (`daemon run/install/start/stop/status/
uninstall`, `inspect`, `version`). launchd's job points at this launcher.

## Layout

```
bin/selly-agent          CLI launcher
src/selly_agent/          the package
tests/                    plain pytest (guards under tests/guard/)
docs/                     this document and friends
Makefile                  local entry points (test, lint, fmt)
```

## The package, by responsibility

Foundations:

- **`paths.py`** — the single path authority. Every location is resolved here,
  honoring the XDG base directories; a guard test enforces that nothing else
  touches home/XDG.
- **`platform/`** — the OS seam (`get_platform()`, `base.Platform`, `macos.py`).
  The "port once" boundary; no launchd string leaks past it.
- **`config.py`** — reads `config.json` (missing → defaults; invalid → rejected;
  unknown keys ignored). The daemon only reads config; the installer writes it.

State — two SQLite databases, kept apart:

- **`db.py`** — WAL, one write connection per database behind a lock, read-only
  connections for readers, explicit transactions.
- **`migrations/`** — one forward-only runner for both databases; numbered SQL
  applied at startup, each in one transaction. The business database is
  snapshotted before pending migrations run.
- **`data/selly.db`** is business data (migrated, snapshotted).
  **`state/events.db`** is the event/transcript store (prunable; recreated from
  migrations if deleted). The two are never joined.
- **`store.py`** — typed accessors over `selly.db`, the one writer for business
  state: items/floors, threads + their transcript, wants/budgets, the sell/buy
  negotiation ledgers, pacing actions, scam signatures, escalations, checkouts,
  seller config, the Q&A bank of answers the seller has taught, the browser
  layer's selector cache, and the pass queue. Two confidentials — the floor and the buyer
  budget — live in their own tables and are never returned by a read an LLM-facing
  tool can call (only the engines load them). Every money/safety decision runs as
  one `BEGIN IMMEDIATE` transaction (load → decide → write), the single-writer
  serialization that gives FCFS single-inventory and atomic pacing. A `ScopedStore`
  wraps the store per request: for a headless pass bound to a `Scope`, every
  thread/want/item row-load must be in scope or it answers exactly as a missing
  row (scope never leaks existence); attended sessions run unscoped. Its stable
  returns are typed (`TypedDict`, checked under `make typecheck`).

The engines — pure decision modules, no I/O, no network. A tool composes an
engine with the store; the engine just decides. Ported from the legacy CLIs with
their tests:

- **`engines/hosts.py`** — the one host-boundary matcher (strict marketplace
  suffix match, link extraction, defang, the config-derived checkout carve-out),
  shared by scam scanning and listing-URL verification. URLs are parsed with pure
  string ops, never `urllib`.
- **`engines/scam.py`** — scam scan scoring + the merged registry∪bank signature
  view (the shipped registry is package data under `data/`; the local bank is a
  store table).
- **`engines/pacing.py`** — the account-safety gate (go/wait/quiet; quiet hours
  and the per-marketplace hourly cap checked before jitter; FAST mode; ceilings as
  code constants). The store's `reserve_action` records at reserve in one
  transaction; the caller sleeps the go-jitter after, never under the lock.
- **`engines/shipping.py`** — the deterministic delivery-fee computation; the
  origin address is never an input.
- **`engines/negotiate.py`** / **`engines/buyer_negotiate.py`** — the sell/buy
  decision ladders, with the never-below-floor / never-above-budget asserts as
  backstops.

Observability:

- **`events.py`** — an in-process bus over a durable store. `publish` stamps the
  journal clock at write; that timestamp is the sole ordering key. Subscribers
  may register (the seam a web tail plugs into later).
- **`retention.py`** — the daily prune (events past a window, snapshots to a
  keep count, logs to a size cap). Kept kinds (`pass.end`) survive the age prune.
- **`inspect_cli.py`** — `selly-agent inspect`, a read-only tail of the event
  store; works whether or not the daemon is running (`--follow` polls). `--json`
  emits NDJSON via the same `events.event_to_wire` serializer the web tail's
  `/events.json` uses. Each event carries a derived `level` (`events.level_for`);
  the tail hides the `routine` heartbeat by default, `--all` shows it.

The tool surface and pass runner — how the LLM touches state and how it runs.
Detail in [`tool-surface-and-passes.md`](tool-surface-and-passes.md):

- **`http_server.py`** — the one localhost HTTP server: the MCP endpoint, the
  web tail, and the pass-control route, on `127.0.0.1` with Host/Origin and
  bearer-token checks.
- **`tools/`** — the typed MCP tool registry: one dispatch path with input
  validation, secret-param masking, and per-session tier filtering. The money and
  safety tools compose the engines with the store; `send_reply` runs the whole
  send bracket (pacing reserve + durable intent in one transaction, the sink send
  outside it, then fold + cursor advance + commit) behind a `ReplySink` seam, which
  the browser layer fills; with no browser available a real market returns a
  structured `no_send_path`. A killed send is folded by the `stale_intent_sweep`
  scheduler task as unconfirmed + an escalation, never re-sent.
- **`mcp_proxy.py`** — a stdio↔HTTP shim so stdio-only harnesses reach the same
  server.
- **`rail/`** — the carousell.ai rail (a stdlib MCP client + guest-key
  provisioning) wrapped behind our tools, off the LLM surface.
- **`browser/`** — the marketplaces the agent drives in Chrome. See below.
- **`harness/`** — the harness seam: one internal `PassSpec`, pure per-provider
  emitters (claude live, codex stub) with round-trip validators. The spec carries
  the pass's web posture, so allowing `WebSearch`/`WebFetch` for a research flow
  is a validated field rather than a hand-edited config.
- **`skills/`** — the prompt layer: the standing rulebooks as package data, plus
  a loader that strips frontmatter and composes a pass type's declared skills
  into one system prompt. See below.
- **`passes.py`** — claims a queued pass single-flight, spawns and babysits a
  headless harness pass, and ledgers its outcome; **`pass_stream.py`** parses the
  harness output stream and **`proc_tree.py`** owns the process-group kill and
  stray-pass reaper.

### The browser layer

Marketplaces with no API are driven through the seller's own logged-in Chrome —
one dedicated profile, one warm browser, CDP open on the loopback interface. The
daemon never launches it: one profile admits exactly one Chrome, so supervision is
launchd's (and in dev, the developer's). `browser/chrome.py` holds the readiness
probe and the launch invocation, and is the layer's only network I/O.

- **`browser/client.py`** — the daemon's own Playwright MCP client, JSON-RPC over a
  stdio subprocess: no port, nothing to authenticate, nothing else on the machine
  can connect to it. Typed errors and no internal retry, the shape `rail/client.py`
  set. `BrowserUnavailable` is distinct because the response is: no Node means the
  daemon runs on with browser lanes skipped, not every market reading as quiet.
- **`browser/markets/`** — the per-market seam. An adapter carries that
  marketplace's JS artifacts, its composer's shipped selectors, its login probe and
  its recipe pointer; everything above depends only on that protocol, so adding a
  marketplace is a new module plus a registry entry. The artifacts are the layer's
  only DOM knowledge and are all class-agnostic — hashed classes churn every
  deploy — locating by role, by href shape, and (for message direction) by geometry.
- **`browser/inbox.py`** — the read lane. It folds buyer messages into durable rows
  for a navigate and one JS evaluate per thread and no model turns at all, which is
  what lets the reply pass above it stay browser-free. Three rules: a market that
  cannot be seen must never look like one with no news (failed reads are counted
  and raise one needs-me notice); the skip gate is a cost optimization backstopped
  by a periodic full sweep, never a correctness input; and reading never advances
  the reply cursor, so a crash between seeing a message and answering it leaves the
  buyer eligible. Every inbound row is scam-scanned as it is written, so the verdict
  is on the row before any model sees the text.
- **`browser/reconcile.py`** — the pure core: a tail read compared against stored
  rows, with whatever is not stored being new. Counting copies of the same text
  against stored rows regardless of what wrote them is what makes a re-read insert
  nothing, and makes our own sent replies and the seller's manual ones reconcile
  rather than double-record.
- **`browser/sink.py`** — the scripted send: navigate the recorded thread URL,
  locate the composer, type, click, stamp, then confirm by reading our own words
  back. The two failure shapes are treated oppositely — nothing sent fails closed
  before the click and stays retryable, while sent-but-unconfirmed stays
  `sent_unverified` and is escalated rather than ever re-driven.
- **`browser/selectors.py`** — shipped selector defaults with the `ui_cache` table
  as a heal overlay over them, so a fresh install pays no vision cost and a
  self-heal never waits on a release. A resolve must match exactly one visible
  element on the right page; none means absent, several means acting would be a
  guess.

One Chrome means three actors share one tab — the read lane, the reply sink, and a
browser-driving pass. A re-entrant mutex on the client serializes whole operations
rather than single calls, and the lane yields entirely while a browser-touching
pass is queued or running.

### Skills and prompt composition

A pass's prompt is split along what changes. The **system prompt** is the
standing rulebook — voice, the listing flow, the house conventions — declared per
pass type and identical across every pass of that type, so a harness can cache
it. The **user prompt** is the task: the rows claimed this time, the item to
publish, the conversation window. The stray-reaper's marker stays in the user
half, where `proc_tree` greps for it.

Skill files are markdown under `skills/`, shipped as package data and read
`__file__`-relative (the `data/marketplaces.json` convention) — a versioned
install serves them from its own tree, a checkout from the checkout. Frontmatter
is stripped when a file is inlined: it is metadata for a human reader, not
instruction for the model. The loader caps the composed size, so a skill added
later cannot quietly inflate every pass of that type.

Which skills a pass type gets is declared on the pass type itself, keeping "a new
pass type is one registry entry" true. The attended surface
(`harness config --attended`) points its slash commands at the same files by
path, through the `current` symlink, so an update changes what a command says
without rewriting it.

### Photos

An item carries an ordered photo list (`{path, uploaded_url?}`, first = cover).
Paths must resolve inside the media store, checked by containment in the single
writer, so a `..` segment or an outward symlink is refused before a row exists.
Photos reach the store two ways: the channel poller downloads them on receipt
(durable before any LLM sees them), and `import_photos` copies local files in for
attended sessions. `carousell_ai_upload_photos` converts what needs converting —
`sips` behind the platform seam, since stdlib cannot transform an image and the
runtime takes no pip dependency — uploads each photo, and stamps the whole set in
one transaction. A partial failure stamps nothing: the marketplace replaces a
photo set wholesale, so half a set is a listing with the wrong cover.

The channel subsystem — the optional bound chat (Telegram today) plus the
needs-me queue that works with none bound. A provider-agnostic core (`channel/`)
with per-provider packages (`channel/telegram/`); a manager starts a provider
only when it's configured or connected, so a daemon with no channel set up runs
no channel thread. Pause lives here too (a paused daemon runs but acts on
nothing). Detail in [`channels.md`](channels.md).

Lifecycle:

- **`lock.py`** — a PID-aware single-instance lock (a live duplicate exits
  clean; a dead holder's lock is reclaimed).
- **`heartbeat.py`** — a `{ts, pid}` file written each tick.
- **`scheduler.py`** — one loop thread submits due tasks to a small pool; a task
  never overlaps itself, every attempt is ledgered, repeated failures back off.
- **`daemon.py`** — the process: lock, ensure dirs, run startup migrations, open
  the bus, run the scheduler; a signal drains cleanly and exits 0.
- **`supervisor.py`** — the OS-agnostic orchestration behind
  `daemon install/start/stop/status/uninstall`.

## Startup, in order

1. Acquire the instance lock (a live duplicate exits 0).
2. Ensure directories; open both databases.
3. Apply pending migrations (snapshot the business database first if any are
   pending); a failure aborts startup.
4. Open the event bus; emit `daemon.start` and one `migration.applied` each.
5. Ensure the attended MCP token; wire the always-on needs-me handlers; start the
   localhost HTTP server (a bind failure is fatal — fail loud so launchd's
   throttle paces respawns).
6. Register the scheduler's tasks, start any configured channel providers, and
   run the loop, writing the heartbeat each tick.
7. On SIGTERM/SIGINT: drain, shut down channel providers, stop the HTTP server,
   emit `daemon.stop`, clear the lock, exit 0.

`daemon run --once` runs a single tick and stops — the deterministic test seam.

## Filesystem locations

Resolved by `paths.py` from the XDG base directories:

```
~/.local/share/selly-agent/   versions/, current -> …, data/selly.db, media/ (business data)
~/.local/state/selly-agent/   events.db, backups/, logs/, passes/, heartbeat, lock (prunable)
~/.config/selly-agent/        config.json + secret files (0700 dir, 0600 secrets)
~/.cache/selly-agent/         downloaded release artifacts
```

Secrets (the attended MCP token, the carousell.ai guest key, the Telegram bot
token) are 0600 files in the config dir, never logged or evented. Inbound channel
media (photo bursts) is downloaded to `share/media/` before any LLM sees it. Per-pass workspaces live under
`state/passes/<pass_id>/` and are swept on pass end. Tests point the XDG
variables at a temporary directory.

## Conventions

- Stdlib only at runtime; dev tools (pytest, ruff, the MCP SDK conformance
  client) live in the `[dev]` extra. A module under `src/` that imports a network
  stdlib package must be added to the guard's network allowlist deliberately.
- Python 3.9 is the floor; ruff is pinned to `py39`. The MCP conformance tests
  need 3.10+ and skip on the floor.
- State changes go through typed code, one writer per store; the LLM reaches
  state only through the typed MCP tools (no Bash in headless passes).
- Guard tests under `tests/guard/` enforce the load-bearing rules.

See `AGENTS.md` for the contributor-facing version of these rules.
