# Observability

One record of what the agent did: the harness's own stream and the server's tool
calls land in a single event store, read by a CLI tail and a localhost web view.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for where this sits in the whole, and
[`tool-surface-and-passes.md`](tool-surface-and-passes.md) for the HTTP routes
that serve the store and the event trail a tool call leaves behind.

## The event store

`events.py` is an in-process bus over a durable SQLite store (`state/events.db`).
`publish(kind, payload, pass_id)` writes first, then fans the stored event out to
any live subscriber; a subscriber that raises never breaks a publish.

- **The journal clock is the only ordering key.** `ts` is stamped at write, by the
  store, never by the caller — a transport's own timestamp rides inside `payload`
  (e.g. `channel.in.src_ts`) and is never used to order anything.
- **The store is disposable.** It is prunable and deletable without data loss;
  never open a transaction across it and `data/sellee.db`. Events are
  observability, not ledger.

### Wire shape

`event_to_wire` is the one serializer: `logs --json` NDJSON lines and the web
tail's `/events.json` rows are byte-identical in shape.

```json
{"@ts": "…", "level": "info", "seq": 1, "ts": 1785…, "pass_id": null, "kind": "…", "payload": {}}
```

`@ts` is a system-local RFC3339 render of `ts`, emitted first for legibility; `ts`
stays the raw epoch. The field order above **is** the wire order — serialize
without `sort_keys` to preserve it.

### Levels

`level` is derived from `kind` at output time (`events.level_for`), never supplied
by the emitter — publishing stays a one-line call. Tiers, low to high:
`routine` → `info` → `warn`. Only the kinds that differ from the `info` default
are listed in `_KIND_LEVELS`, so a newly added kind is visible until someone
deliberately demotes it. Today that list is the scheduler heartbeat
(`task.start`/`task.ok` → `routine`) and its failures (`task.error`/`task.backoff`
→ `warn`).

The tier is load-bearing twice: consumers hide the bottom rung by default, and
retention ages it out on a shorter window.

### Retention

`retention.py` runs a daily prune: events past the long window, the `routine`
tier past a short one, DB snapshots to a keep count, logs to a size cap. Kept
kinds (`pass.end`) survive the age prune, so a pass ledger outlives its detail.

## Correlating one unit of work

Everything a pass does carries its `pass_id`. Within that, two independent id
families pair a call with its outcome — worth having because adjacency stops
being a reliable signal the moment two passes interleave:

| Family | Ids | Emitted by |
|---|---|---|
| Harness (what the model saw) | `pass.tool_use.id` ↔ `pass.tool_result.tool_use_id` | `pass_stream.py` |
| Server (ground truth) | `call_id` on `tool.call` / `tool.result` / `tool.error` | `tools/registry.py` |

A user message becomes **one `pass.tool_result` event per block**, so each carries
its own `tool_use_id`; content that is not a `tool_result` block stays a single
blob with no such key.

**The two families never mix.** A harness `toolu_…` id never reaches the server,
so `pass.tool_use` and `tool.call` are two separate records of the same call, not
something to fold together. The one `tool.error` with no `call_id` is the
validation failure — it fires before a call exists, so there is nothing to pair.

## `sellee logs`

A read-only tail over its own connection: it needs no cooperation from the daemon
and works whether or not one is running, including against a stopped daemon's
history (WAL gives concurrent readers by construction).

| Flag | Effect |
|---|---|
| `--follow` | poll `seq >` last on a ~1s cadence |
| `--json` | NDJSON, one event per line — the `jq`-able form |
| `--since 30s\|15m\|2h\|1d` | start at a time offset rather than the beginning |
| `--pass <id>` | one pass's events, harness and server interleaved |
| `--kind <k>` | an explicit request, so it shows whatever it asks for regardless of level |
| `--all` | lift the level floor to include the `routine` heartbeat |
| `--web` | open the web tail below instead of printing (needs the daemon) |

Without `--all` or `--kind`, the floor is `info`: the heartbeat is hidden. The
text format is one line per event (`time  kind  pass=…  payload`) and is
deliberately plain — colorized machine output is `--json` piped through `jq -c`.

`--web` composes only with `--since`. The other flags are refused rather than
silently ignored — the page has its own equivalents.

## The web tail

`http://127.0.0.1:<http_port>/tail?token=<attended-token>` (the token is a 0600
file in the config dir); `sellee logs --web` composes and opens it. The page
is `data/tail.html`, a packaged asset read per request; it polls `/events.json`
and appends to the DOM.

It opens on the **newest** events rather than the oldest, so one request seeds
what you see and an idle agent still opens on what it last did. The `routine`
tier never reaches this page at all — the heartbeat is most of the volume and none
of what a tail is opened to read; `logs --all` is where it is answered.

This is the **human** surface, and the split is deliberate: the NDJSON stream
above stays the canonical machine form *and* the debugging tool, which is what
frees the page to hide and abbreviate aggressively. Every row it shows expands to
its own wire JSON, and one toggle reveals the rest.

### A row

Time, an actor pill, the rendered event, and a `{}` disclosure holding the raw
wire JSON. The pill names who acted, and colors the row's left border to match:

- **a pass** — `hash(pass_id) → hue`, so one pass's events read as a group. Click
  it to isolate that pass; a chip in the header clears the filter.
- **`user`** (teal) — `channel.in`, the seller's own voice.
- **`system`** (grey) — every other event with no pass id: the daemon acting on
  its own.

### Renderers

One renderer per `kind`, keyed off a table. The seller's messages and the agent's
replies read as a conversation; a tool call collapses into a single row with its
result folded in, paired per `pass_id` on the ids above (an unmatched result
stands alone rather than being dropped). **An unrecognized kind falls back to a
compact line** — a kind added elsewhere in the codebase degrades gracefully here
instead of going invisible.

Renderers build DOM through `createElement`/`textContent` only. Payloads carry
text a buyer wrote, so it never reaches the page as markup — by construction,
not by escaping.

### Controls

| Control | Behavior |
|---|---|
| **follow** | On by default: new rows keep the viewport at the bottom. Scrolling up hands control back; scrolling to the bottom, or ticking the box, resumes. |
| **verbose** | Reveals what is hidden by default — thinking-token ticks and the kinds redundant beside the rows they accompany. Not the `routine` tier, which never reaches the page. |
| **pass pill** | Isolates one pass (see above). |
| `?since=` | Narrows the load to a lookback window, in the `--since` grammar. No default: a load opens on the newest events whatever their age. |
| `?json=true` | The zero-renderer view: one raw JSON line per event, matching `logs --json`. |

Thinking *content* never enters the log at all — the stream parser drops those
blocks — so the per-tick `pass.raw` marker is the only thinking artifact, and the
renderer hides it client-side rather than demoting its level.
