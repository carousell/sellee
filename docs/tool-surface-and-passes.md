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
  event store over a read-only connection. Rows use the shared
  `events.event_to_wire` shape (same as `inspect --json`, incl. the derived
  `level`); `/tail?...&json=true`
  renders that raw JSON per line.
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
  listing exactly the pass's rules (last, since the flag greedily consumes what
  follows). Stream-json output requires `--verbose` with `-p` (a hard CLI
  requirement). The spec's composed system prompt rides
  `--append-system-prompt`. `readable_paths` renders as one `Read(//abs/path)`
  rule per file; the bare `Read` deny is emitted only when nothing is granted,
  because a deny overrides any allow. Unmatched reads still fail — a headless
  session rejects unmatched tools by default.

**Web posture.** `WebSearch`/`WebFetch` are denied by default and allowed only
for a pass whose type sets `PassSpec.web_tools` — the emitter moves the two names
between the allow and deny lists, so the posture goes through the round-trip
validators rather than being hand-written into a config. Two validators cover it:
a pass without web tools must deny them *explicitly* (silence is not a posture),
and no tool may appear in both lists. Bash and the file-access tools are denied
whatever the flag says.

**File posture.** A model can only *see* an image by reading the file, so a
photo-handling pass needs eyes on its photos without gaining file access in
general. `PassSpec.readable_paths` lists exactly the media files claimed into
the pass (containment-checked against the media store at spec build); nothing
else on the filesystem is readable. How that is enforced is each emitter's
business, but its validators must pin both directions — no grants → file reads
impossible; grants → those files and nothing wider — so a harness that can't
express a per-file grant fails at render rather than shipping a looser posture.
- **codex** (stub) renders `config.toml` pointing at `mcp-proxy`; there is no
  spawn path yet. Keeping a second real emitter forces the internal representation
  to stay genuinely common.

## Pass types

`passes.PASS_TYPES` maps a pass type to its tier, its skills, its web posture,
and a prompt builder, so a new type registers there rather than forking the
runner:

| type | tier | skills | web | browser | scope |
|---|---|---|---|---|---|
| `publish` | `pass:publish` — `get_item`, the photo/publish pair, `send_message`, and the selector cache (`ui_cache_*`, `probe_selector`) | conventions + the market's own recipe | no | for a browser market only | full |
| `reply` | `pass:reply` — its own threads and items, `negotiate_offer`/`status`, `search_qa_bank`, `send_reply`, `hold_thread`, `escalate`, `quote_shipping`, the checkout link, `scam_scan` | conventions, voice-and-style, buyer-conversation, scam-guard | no | no | its claimed threads + items |
| `channel` | `pass:channel` — the broad seller-conversation set (items, photos, floors, threads, negotiate, checkout, escalations, settings, the Q&A bank, `send_message`, …) | conventions, voice-and-style, seller-comms, listing-flow | yes | no | full |

All three tiers are pinned by a golden (`tests/golden/pass_tiers.json`), so
widening one is a deliberate diff. Membership follows what the skills instruct: a
tool no skill tells a pass to use is surface with no user, and a tool a skill
needs but the tier omits is a flow that dead-ends mid-conversation.

- **`publish`** publishes one already-confirmed item — it talks to no one, so it
  gets no voice rulebook and no web. Which recipe it carries, and whether it is
  handed a browser at all, come from the market in its payload: the rail is an API
  call, a browser market is a form to fill. Browser authority follows the market,
  not the pass type.
- **`reply`** answers buyers, and is the most constrained flow in the system
  because it is the only one acting on words a stranger wrote. It runs **scoped**
  to the threads the lane claimed for it, so another buyer's thread reads as
  absent rather than forbidden. It has no web research and no browser: the send
  goes out through the daemon's own sink, which the model never touches. Absent by
  design is anything that writes on the seller's behalf — banking an answer,
  confirming a sale, recording a scam signature — since it has only ever heard the
  buyer's side.
- **`channel`** is the phone-driven sell conversation. It runs **full-scope** (the
  counterpart is the trusted seller, not a buyer). Its prompt embeds a
  **recent-transcript window** — inbox rows interleaved with the agent's own
  notices, capped by count and chars — so a follow-up like "yes, do that"
  resolves, clearly separated from the messages to handle now. Photo rows carry
  their stored media paths inline, so the listing flow can attach them directly.
  Being the flow that holds the seller's words, it is also the one that banks a
  taught answer, records a confirmed scam signature, and relays either to a buyer.

## The second MCP server

A pass that drives the browser reaches a second MCP server: its own Playwright
process, spawned by the harness over **stdio**, inside the pass's process group so
it dies with the pass. Not an HTTP instance — a localhost browser-control port
would be an unauthenticated way to drive the seller's Chrome.

Because `--strict-mcp-config` is in force, the rendered server set *is* the pass's
reachable surface, so all three round-trip validators check every server and
reject one the spec did not ask for. Reaching the server is necessary but not
sufficient: the allow-list carries a **diet** of ~12 browser tools. `browser_close`
(it would shut the seller's warm Chrome), `browser_run_code_unsafe` (arbitrary
Playwright code — the browser's equivalent of the shell this surface replaces) and
`browser_take_screenshot` (every check is a DOM read-back) are all deliberately
out.

## Prompt composition

A pass's prompt is split in two, along the axis of what changes:

- The **system prompt** is the standing rulebook — the skills the pass type
  declares, concatenated in order with their frontmatter stripped. It is
  identical across every pass of that type, so a harness can cache it.
- The **user prompt** is the task: what to do this time, the claimed rows, the
  conversation window. It also carries the marker `proc_tree` greps for when
  reaping strays, which is why the marker stays out of the system half.

Skills live as package data under `skills/` and load `__file__`-relative, so a
versioned install serves them from its own tree. The loader caps the composed
size: every skill added to a pass type is paid on every pass of that type, and
that should fail a test rather than quietly inflate the bill.

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
