# The MCP tool surface and pass runner

How the LLM touches state, and how it runs. This is the "vertical slice": the
daemon spawns a headless harness pass, the pass calls back over MCP, and every
call and event lands in the transcript store correlated by one pass id. See
[`ARCHITECTURE.md`](ARCHITECTURE.md) for where these modules sit in the whole.

## The HTTP server

One `ThreadingHTTPServer` bound to `127.0.0.1` (`http_server.py`), serving three
surfaces:

- **`POST /mcp`** — stateless MCP over streamable HTTP: JSON-RPC 2.0 with
  `initialize`, `notifications/initialized`, `tools/list`, `tools/call`, `ping`.
  Responses are plain JSON (no SSE); `GET /mcp` is 405.
- **`GET /events.json` + `GET /tail`** — the localhost web tail, reading the
  event store over a read-only connection.
- **`POST /control/enqueue-pass`** — enqueue a pass (attended token only).
- **`POST /control/connect-telegram`** + **`GET /control/channel-status`** — the
  Telegram bind flow (attended token only): the connect route takes the BotFather
  token, validates it, writes it 0600, mints a bind nonce, and returns the
  `t.me/<bot>?start=<nonce>` deep link; the status route reports off /
  awaiting-bind / bound for the connecting CLI to poll. The token is never echoed
  or logged; only `bot_username` is published (`channel.bind_attempt`).

Hardening applies to every request: the Host must be a localhost name and any
Origin header a localhost origin (DNS-rebinding defense), and a bearer token must
resolve to a session. Auth is two-tier: one **persistent attended token** (a
config-dir secret) and **per-pass ephemeral tokens** minted at spawn and revoked
at pass end. Comparison is constant-time and a 401 never echoes the presented
token.

## The tool registry

A `ToolSpec` is declarative data: name, an input schema (a small hand-rolled
JSON-Schema subset — stdlib-only rules out `jsonschema`), the names of its secret
parameters, the session tiers allowed to see it, and a handler. Tools register at
import.

Every call flows through one `dispatch` path so the load-bearing rules hold
uniformly:

- **Validation** rejects unknown params rather than dropping them.
- **Secret masking** replaces marked params in a copied payload before anything
  reaches the event bus — a marked value never lands in a sink.
- **Tier filtering** gates both `tools/list` and `tools/call`: a tool a session's
  tier can't see is indistinguishable from one that doesn't exist.
- Each call publishes `tool.call`, then `tool.result` or `tool.error`, keyed by
  the session's pass id — the server-side ground truth against model narration.

Tool-level failures come back as `tools/call` results with `isError`, not
transport errors; only malformed/unknown JSON-RPC is a protocol error.

The **carousell.ai rail** (`rail/`) is wrapped behind tools and never appears on
the LLM surface: a stdlib MCP client over `urllib` (the guest key travels only in
the Authorization header), a fail-closed live listing-URL verify, and guest-key
provisioning. `mcp_proxy.py` is a stdio↔HTTP forwarder so a stdio-only harness
reaches the same server — the HTTP server stays the single implementation.

## The harness seam

One internal `PassSpec` describes a pass; pure per-provider emitters render it,
and each parses its own output back and asserts it matches (reject, never
sanitize):

- **claude** (live) renders the `claude -p` argv plus the workspace's
  `.mcp.json` and `.claude/settings.json`. The no-Bash posture is by
  construction: `--strict-mcp-config` with only our server, and `--allowedTools`
  listing exactly the tier's `mcp__<server>__*` names (last, since the flag
  greedily consumes what follows). Stream-json output requires `--verbose` with
  `-p` (a hard CLI requirement).
- **codex** (stub) renders `config.toml` pointing at `mcp-proxy`; there is no
  spawn path yet. Keeping a second real emitter forces the internal representation
  to stay genuinely common.

## Pass types

`passes.PASS_TYPES` maps a pass type to its tier and a prompt builder, so a new
type registers there rather than forking the runner:

- **`publish`** (`pass:publish`) — the vertical-slice type: publish one item.
- **`channel`** (`pass:channel`) — the phone-driven sell conversation. It runs
  **full-scope** (the counterpart is the trusted seller, not a buyer), so its
  tier is a broad set (items, floors, threads, negotiate, escalations,
  `send_message`, …). Its prompt embeds a **recent-transcript window** — inbox
  rows interleaved with the agent's own notices, capped by count and chars — so a
  follow-up like "yes, do that" resolves, clearly separated from the messages to
  handle now. The interim prompt is throwaway; the skills rewrite replaces it and
  finalizes tier membership.

`send_message` and `get_catchup` are the channel's tool seam. `send_message`
inserts a durable notice and returns `{queued: true, notice_id}` — it never forks
on binding state, so an unbound send simply queues for catchup. `get_catchup` is
the needs-me read (open escalations + queued notices + channel/pause state +
connect hint); returning the queued notices delivers them, so it stamps them
delivered-via-catchup, while escalations clear only on resolve.

## A pass, end to end

1. `pass run publish --item X` posts to `POST /control/enqueue-pass`; a `queued`
   row is inserted and a pass id returned. (A `channel` pass is enqueued instead
   by the poller, which claims the pending inbox rows into it in one transaction.)
2. The scheduler's pass lane claims it **single-flight** (stamping `running` in
   one transaction), mints an ephemeral pass-tier token, writes an empty per-pass
   workspace holding only the generated harness config, and spawns the harness.
3. The pass calls back over `POST /mcp` with its token; each call is validated,
   tier-filtered, and logged server-side. The pass's stdout is parsed live
   (`pass_stream.py`) into `pass.*` events.
4. A babysitter enforces the deadline and daemon-stop via a process-group kill
   (`proc_tree.py`). On exit the outcome is classified
   (`ok`/`error`/`timeout`/`cap_hit`/`spawn_error`) and ledgered as `pass.end`
   (a retention keep-kind); the token is revoked and the workspace swept.

A crash mid-pass is failed loudly by a stale-running sweep (never silently
re-run), and a stray reaper kills untracked marked pass groups past their
deadline. `inspect --pass <id>` interleaves the harness stream and the
server-side tool events by their shared pass id.
