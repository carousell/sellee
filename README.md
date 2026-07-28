# selly-agent

A local, single-tenant agent that sells and buys on peer-to-peer marketplaces
from the user's own machine. It runs as one always-on Python process under
launchd, exposes a typed tool surface to an LLM harness, drives a real
logged-in browser, and talks to the user over an optional chat channel.

This repository is the rewrite of the original implementation. It is a
greenfield core that ports the battle-tested engines from the legacy repo.

## Status

Early. On top of the core skeleton (XDG paths, config, a SQLite state layer with
a startup migration runner, an event bus + transcript store, a scheduler loop,
launchd integration, and the `inspect` CLI), the daemon now runs the **vertical
slice**: a localhost HTTP server exposing a typed MCP tool surface, a pass runner
that spawns a headless `claude -p` pass and streams it to the event bus, the
carousell.ai rail wrapped behind a tool, and the harness-config seam. The
optional **Telegram channel** now lands too — nonce bind, a durable inbox,
deterministic fast paths, the needs-me queue (escalations + notices), and a
phone-driven channel pass. The browser layer and the skills rewrite are later
workstreams.

## Where the plans live

Design, architecture decisions, invariants, and the plan this code implements
are tracked in a separate **projects repo** (not here) — this repo holds code
only. If you are implementing against a plan, start there. For a high-level map
of how this repo is put together, see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Runtime constraints

- **Python stdlib only at runtime.** The user's own `python3` is the only
  runtime dependency — there is no pip install step on a user machine. A guard
  test fails the suite if any module under `src/` imports a non-stdlib package.
- **Python 3.9 is the floor.** macOS Command Line Tools ship 3.9; the suite
  must pass on it. In practice: `from __future__ import annotations` in every
  module, no `match`, no runtime `X | Y` unions (annotations are fine), no
  `tomllib`.

## Dev quickstart

Dev/test tooling (pytest, ruff) lives in the `[dev]` extra — never under
`src/`.

```sh
make test            # pytest on the current interpreter
make test-3.9        # the suite on a 3.9 interpreter (skips with a note if absent)
make lint            # ruff check + ruff format --check
make fmt             # ruff format

# run the daemon in the foreground (lock -> migrate -> serve MCP + run the pass lane)
bin/selly-agent daemon run

# provision the carousell.ai guest key (once), then publish an item via a headless pass
bin/selly-agent provision carousell-ai --region SG
bin/selly-agent pass run publish --item <item_id> --follow

# set up an attended Claude Code session against the same daemon MCP server:
# .mcp.json, the /sell /selly /catchup /pause /resume commands, and a CLAUDE.md.
# Then, in that directory: `claude`, and `/sell` to list something.
bin/selly-agent harness config --attended --dir /path/to/session

# connect the optional Telegram channel. Run it interactively and it prints BotFather
# guidance, then prompts (hidden) for the token; open the printed t.me/<bot>?start=<nonce>
# deep link on the phone that has Telegram (a bare /start won't bind). Re-runnable, and
# `--status` just reports the bind state. Scripted? Pipe the token on stdin instead:
bin/selly-agent connect telegram                          # interactive prompt
printf '%s' "<bot-token>" | bin/selly-agent connect telegram   # scripted

# tail the event store (works whether or not the daemon is running)
bin/selly-agent inspect --follow

# or NDJSON — one event object per line, pipeable to jq
bin/selly-agent inspect --follow --json | jq .

# routine heartbeat events (task.start/task.ok) are hidden by default; --all shows them
bin/selly-agent inspect --all
```

Once bound, the phone is the async channel: buyer escalations push to it, and
`/pause` · `/resume` · `/status` · `/catchup` · `/selly` are answered instantly
by the daemon (no LLM); anything else is a conversation with your selling agent.

The daemon also serves a localhost web tail at
`http://127.0.0.1:<http_port>/tail?token=<attended-token>` (the token lives 0600 in the config
dir) — a rendered, human-readable view of the same event log, where `inspect --json` is the
machine form. Both are covered in [`docs/observability.md`](docs/observability.md).

Tests point `$XDG_*_HOME` at a tmpdir, so they never touch a real install.

## Layout

```
bin/selly-agent            single CLI launcher (resolves src/, dispatches argv)
src/selly_agent/
  cli.py                   argparse dispatch (daemon, inspect, pass, harness, provision, …)
  paths.py                 the one path authority (XDG; only module touching home/XDG)
  platform/                OS seam (macOS launchd; Windows is a later port)
  config.py                read-only config.json loader (+ installer-side writer)
  secrets.py               config-dir secret files (0600): MCP token, carousell.ai key
  db.py                    SQLite: WAL, one write connection per DB, readers
  migrations/              forward-only numbered SQL migrations + the runner
  store.py                 typed accessors over selly.db (items, floors, pass queue)
  events.py                event bus + transcript store (the observability record)
  retention.py             daily prune task
  http_server.py           localhost HTTP: MCP endpoint + web tail + pass control
  mcp_proxy.py             stdio<->HTTP MCP shim for stdio-only harnesses
  tools/                   the typed MCP tool registry + the tool implementations
  channel/                 provider-agnostic channel core + channel/telegram/ provider
  connect_cli.py           `connect telegram` (token via stdin -> control route)
  rail/                    the carousell.ai rail client + guest-key provisioning
  harness/                 the harness seam: PassSpec + claude/codex emitters
  skills/                  the prompt layer: skill markdown + attended command bodies
  passes.py                the pass runner (claim -> spawn -> babysit -> classify)
  pass_stream.py           stream-json -> common event schema
  proc_tree.py             process-group kill + stray-pass reaper
  pass_cli.py              pass run / harness config / provision CLI verbs
  lock.py                  PID-aware single-instance lock
  heartbeat.py             liveness heartbeat file
  scheduler.py             the loop: due tasks -> executor, backoff, task events
  daemon.py                wires it together; the daemon process
  supervisor.py            launchd install/start/stop/status/uninstall
  inspect_cli.py           the event tail
  data/tail.html           the web tail's page: the rendered human view of the event log
tests/                     plain pytest (tests/conformance/ = MCP SDK interop, 3.10+)
```

## Filesystem locations (XDG)

```
~/.local/share/selly-agent/   versions/, current -> …, data/selly.db, media/
~/.local/state/selly-agent/   events.db, backups/, logs/, heartbeat, lock
~/.config/selly-agent/        config.json + secrets 0600 (MCP, carousell.ai, telegram token)
~/.cache/selly-agent/         downloaded release tarballs
```
