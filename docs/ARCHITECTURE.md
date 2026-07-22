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
  seller config, and the pass queue. Two confidentials — the floor and the buyer
  budget — live in their own tables and are never returned by a read an LLM-facing
  tool can call (only the engines load them). Every money/safety decision runs as
  one `BEGIN IMMEDIATE` transaction (load → decide → write), the single-writer
  serialization that gives FCFS single-inventory and atomic pacing. A `ScopedStore`
  wraps the store per request: for a headless pass bound to a `Scope`, every
  thread/want/item row-load must be in scope or it answers exactly as a missing
  row (scope never leaks existence); attended sessions run unscoped. The stable
  record and ack returns are `TypedDict`s (a dict at runtime, a checked shape under
  `make typecheck`); the two acks are structurally value-free — `FloorAck`/`BudgetAck`
  have no value field, so the checker proves an ack cannot carry the secret out.

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
  store; works whether or not the daemon is running (`--follow` polls).

The tool surface and pass runner — how the LLM touches state and how it runs.
Detail in [`tool-surface-and-passes.md`](tool-surface-and-passes.md):

- **`http_server.py`** — the one localhost HTTP server: the MCP endpoint, the
  web tail, and the pass-control route, on `127.0.0.1` with Host/Origin and
  bearer-token checks.
- **`tools/`** — the typed MCP tool registry: one dispatch path with input
  validation, secret-param masking, and per-session tier filtering. The money and
  safety tools compose the engines with the store; `send_reply` runs the whole
  send bracket (pacing reserve + durable intent in one transaction, the sink send
  outside it, then fold + cursor advance + commit) behind a `ReplySink` seam —
  04 ships no live sink, so a real market returns a structured `no_send_path`. A
  killed send is folded by the `stale_intent_sweep` scheduler task as unconfirmed
  + an escalation, never re-sent.
- **`mcp_proxy.py`** — a stdio↔HTTP shim so stdio-only harnesses reach the same
  server.
- **`rail/`** — the carousell.ai rail (a stdlib MCP client + guest-key
  provisioning) wrapped behind our tools, off the LLM surface.
- **`harness/`** — the harness seam: one internal `PassSpec`, pure per-provider
  emitters (claude live, codex stub) with round-trip validators.
- **`passes.py`** — claims a queued pass single-flight, spawns and babysits a
  headless harness pass, and ledgers its outcome; **`pass_stream.py`** parses the
  harness output stream and **`proc_tree.py`** owns the process-group kill and
  stray-pass reaper.

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
5. Ensure the attended MCP token; start the localhost HTTP server (a bind
   failure is fatal — fail loud so launchd's throttle paces respawns).
6. Register tasks (retention, the pass lane, the stray reaper, the stale-intent
   sweep) and run the scheduler, writing the heartbeat each tick.
7. On SIGTERM/SIGINT: drain, stop the HTTP server, emit `daemon.stop`, clear the
   lock, exit 0.

`daemon run --once` runs a single tick and stops — the deterministic test seam.

## Filesystem locations

Resolved by `paths.py` from the XDG base directories:

```
~/.local/share/selly-agent/   versions/, current -> …, data/selly.db (business data)
~/.local/state/selly-agent/   events.db, backups/, logs/, passes/, heartbeat, lock (prunable)
~/.config/selly-agent/        config.json + secret files (0700 dir, 0600 secrets)
~/.cache/selly-agent/         downloaded release artifacts
```

Secrets (the attended MCP token, the carousell.ai guest key) are 0600 files in
the config dir, never logged or evented. Per-pass workspaces live under
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
