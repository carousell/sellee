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
  `events.event_to_wire` shape (same as `logs --json`, incl. the derived
  `level`); `after_seq` pages, `pass` filters, and `since_sec` windows the
  lookback so a page load starts near now instead of replaying the retention
  window. `/tail` serves `data/tail.html`, the rendered human view;
  `/tail?...&json=true` renders that raw JSON per line instead.
- **`POST /control/enqueue-pass`** — enqueue a pass (attended token only).
- **`POST /control/connect-telegram`** + **`GET /control/channel-status`** — the
  Telegram bind flow (attended token only): the connect route takes the BotFather
  token, validates it, writes it 0600, mints a bind nonce, and returns the
  `t.me/<bot>?start=<nonce>` deep link; the status route reports off /
  awaiting-bind / bound for the connecting CLI to poll, plus the `adapter` those
  states belong to (one provider binds at a time — see `docs/channels.md`). The
  token is never echoed or logged; only `bot_username` is published
  (`channel.bind_attempt`).
- **`POST /control/settings-set`** — set a setting outright (attended token
  only). Same registry parser and same `check_for_seller` as the propose path;
  what it skips is the approval round-trip, which exists to gate the model rather
  than the person typing. The prior value is recorded, so Undo still works.
- **`POST` / `GET /control/seller-basics`** — write and read region, currency and
  timezone. The installer needs the region before it can provision the rail, and
  asks the daemon for it rather than opening a database a live process owns.
- **`POST /control/connect-market`**, **`GET /control/market-login`**,
  **`GET /control/market-logins`** — open a marketplace in the agent's Chrome for
  the seller to sign in, probe one market's login, and report every enabled market
  at once for the healthcheck. The plural read declines to probe when Chrome is
  closed: acquiring the browser starts it, and a status read that opens a window
  on someone's screen is not a status read. The connect route may start Chrome —
  that is what it is for — but answers 409 while a pass is driving the shared
  tab, rather than navigating it out from under a half-filled composer.

Every one of these is called through `control.py`, the one client for them —
which is also why the stdlib guard grants socket capability to that module rather
than to each CLI verb in turn.

Hardening applies to every request: the Host must be a localhost name and any
Origin header a localhost origin (DNS-rebinding defense), and a bearer token must
resolve to a session. Auth is two-tier: one **persistent attended token** (a
config-dir secret) and **per-pass ephemeral tokens** minted at spawn and revoked
at pass end. Comparison is constant-time and a 401 never echoes the presented
token.

## The tool registry

A `ToolSpec` is declarative data: name, an input schema (a small hand-rolled
JSON-Schema subset — not worth a dependency), the names of its secret
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
  All three carry a `call_id` (`call_<hex12>`) minted per call, so a reader pairs
  a call with its outcome exactly rather than by adjacency. The one exception is
  the `tool.error` from a *validation* failure: no call happened, so there is
  nothing to pair with.

Tool-level failures come back as `tools/call` results with `isError`, not
transport errors; only malformed/unknown JSON-RPC is a protocol error.

The **carousell.ai rail** (`rail/`) is wrapped behind tools and never appears on
the LLM surface: a stdlib MCP client over `urllib` (the guest key travels only in
the Authorization header), a fail-closed live listing-URL verify, and guest-key
provisioning. `mcp_proxy.py` is a stdio↔HTTP forwarder so a stdio-only harness
reaches the same server — the HTTP server stays the single implementation.
`rail.update_listing` has two callers with disjoint arguments: the
`carousell_ai_update_listing` tool (take-down) passes status only, and the daemon's
cross-link push passes `external_urls` only — that field is daemon-owned, and no
tool writes it.

## The harness seam

One internal `PassSpec` describes a pass; pure per-provider emitters render it,
and each parses its own output back and asserts it matches (reject, never
sanitize):

- **claude** (live) renders the `claude -p` argv plus the workspace's
  `.mcp.json` and `.claude/settings.json`. The no-Bash posture is by
  construction: `--strict-mcp-config`, so the rendered server set is the whole set
  (our server, plus the Playwright server for a browser-driving pass — see
  *Multiple servers* below), and `--allowedTools` listing exactly the pass's rules
  (last, since the flag greedily consumes what follows). Stream-json output requires `--verbose` with `-p` (a hard CLI
  requirement). `-p` is bare and the runner writes the prompt to the process's
  stdin, so a buyer conversation never lands in `ps` output. The composed system
  prompt does ride `--append-system-prompt`, since it is static skill text with
  no seller or buyer data in it.
  `readable_paths` renders as one `Read(//abs/path)` rule per file; the bare
  `Read` deny is emitted only when nothing is granted,
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
| `publish` | `pass:publish` — `get_item`, the photo/publish pair, `record_published_listing_url` (how a browser publish's result gets back at all), `send_message`, and the selector cache (`ui_cache_*`, `probe_selector`) | conventions + the market's own recipe | no | for a browser market only | full |
| `reply` | `pass:reply` — its own threads and items, `negotiate_offer`/`status`, `search_qa_bank`, `send_reply`, `hold_thread`, `escalate`, `quote_shipping`, the checkout link, `scam_scan` | conventions, voice-and-style, buyer-conversation, scam-guard | no | no | its claimed threads + items |
| `channel` | `pass:channel` — the broad seller-conversation set (items, photos, floors, threads, negotiate, checkout, `carousell_ai_create_signin_link`, escalations, settings, the Q&A bank, `carousell_ai_update_listing`, `queue_marketplace_publish`, `send_message`, …) | conventions, voice-and-style, seller-comms, listing-flow | yes | no | full |

`carousell_ai_create_signin_link` — which mints the seller's one-time
carousell.ai sign-in URL, the thing standing between a guest account and any
checkout link — is deliberately absent from `reply`. That URL grants ownership of
the seller's account, including the live API key, so a buyer-facing pass must
structurally be unable to hold it; when the gate fires there, the pass holds the
buyer with a neutral line and escalates instead, and the seller's own channel
mints the link.

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
  to the threads the lane claimed for it, so a thread outside the pass reads as
  absent rather than forbidden. The scope is per **pass**, not per thread: a
  burst of buyers is deliberately coalesced into one pass over one scope, so
  within a pass those conversations share a prompt and a scope, and the bound is
  "the threads this pass claimed" rather than per-buyer isolation. It has no web research and no browser: the send
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
   by the poller, which claims the pending inbox rows into it in one transaction;
   a fan-out `publish` by the crosslist lane, which marks its payload
   `origin: "crosslist"` so the outcome is reported to the seller — a pass run by
   hand is watched by whoever ran it and is not.) A publish payload naming a
   market that cannot be published to — unknown, no adapter, no recipe, or no site
   in the seller's region — is refused here with a 400 rather than ledgered as a
   pass that could only fail.
2. The scheduler's pass lane claims it **single-flight** (stamping `running` in
   one transaction), mints an ephemeral pass-tier token, writes an empty per-pass
   workspace holding only the generated harness config, and spawns the harness.
3. The pass calls back over `POST /mcp` with its token; each call is validated,
   tier-filtered, and logged server-side. The pass's stdout is parsed live
   (`pass_stream.py`) into `pass.*` events: `pass.tool_use` carries the harness
   block's `id`, and a user message becomes one `pass.tool_result` per block
   carrying its `tool_use_id`, so the harness view pairs on its own ids too.
   The two id families never mix — a harness `toolu_…` never reaches the server,
   so `pass.tool_use` and `tool.call` stay separate records of the same call.
4. A babysitter enforces the deadline and daemon-stop via a process-group kill
   (`proc_tree.py`). On exit the outcome is classified
   (`ok`/`error`/`timeout`/`cap_hit`/`spawn_error`) and ledgered as `pass.end`
   (a retention keep-kind); the token is revoked and the workspace swept.

A crash mid-pass is failed loudly by a stale-running sweep (never silently
re-run), and a stray reaper kills untracked marked pass groups past their
deadline. `logs --pass <id>` interleaves the harness stream and the
server-side tool events by their shared pass id.
