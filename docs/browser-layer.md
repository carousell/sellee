# The browser layer

Most marketplaces have no third-party APIs, so the agent interacts with them the way a human would, by controlling a browser. This is the layer that does it. Playwright is required for controlling the browser. Only Chrome browser is supported.

It is an **optional** layer. A agent with no third-party marketplaces will continue posting listings on carousell.ai.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for where this sits in the whole, and
[`tool-surface-and-passes.md`](tool-surface-and-passes.md) for the *other* browser
seam — the Playwright server a publish pass drives itself, which is separate from
everything below.

## The browser (Chrome)

A dedicated Chrome profile is used for all agent interactions. This prevents the agent from seeing the user's everyday web-browsing sessions. The agent interacts with the browser using Chrome DevTools Protocol (CDP).

`browser/chrome.py` starts the browser when nothing is listening. The probe decides
everything: a port that answers is left alone (two Chromes cannot share a profile),
and only a silent port makes it safe to clear a `Singleton*` lock left by a killed
Chrome and launch. Chrome gets its own session, so neither the daemon exiting nor a
pass being killed takes the seller's browser with it.

**Acquiring the browser means ensuring it runs.** Every actor that needs the browser
— the read lane, the reply send, the selector probe, the fan-out — acquires it through
the daemon's one factory, and the factory checks Node, brings Chrome up if the port is
silent, and tells the seller a window is coming (the notice names no flow, because any
actor may be the one that opens it). A Chrome that will not start surfaces as
`BrowserUnavailable` carrying the by-hand launch command, which every caller already
handles. A launch is serialized under one lock (two callers in the same window must
not both launch onto one profile), and a launch that failed quiets further attempts
for five minutes rather than costing every caller the full wait. Keeping Chrome alive
across crashes and logins is the supervisor's job.

**Except where the browser is on a different machine.** In the container install
profile (`docs/docker.md`) the daemon runs in a container and Chrome runs on the
seller's own desktop, so acquiring it is `ensure_running(may_launch=False)`: the probe,
and nothing else. No binary is resolved, no `Singleton*` lock is cleared, nothing is
spawned — each of those would act on the wrong machine's Chrome. A silent port is
`UNAVAILABLE` immediately, with a hint naming the launch script the seller runs
themselves, and `LAUNCHED` simply never happens, so the window notice is never queued.
With no launch there is no failed-launch backoff either: every acquisition just
re-probes, which is cheaper than the host path rather than more expensive.

The endpoint stays `http://127.0.0.1:<port>` on both sides of that boundary, and this
is load-bearing rather than incidental. Chrome refuses a `/json*` request whose `Host`
header is a DNS name (its DNS-rebinding protection) and Playwright's first act is
exactly that request; the `webSocketDebuggerUrl` it answers with is loopback-shaped and
Playwright dials it verbatim. So the container forwards its own `127.0.0.1:9222` to the
host rather than naming the host, and nothing in this file — not `cdp_endpoint`, not
`browser_command`, not the `.mcp.json` emitter — has a deployment branch in it.

## The client

`browser/client.py` is the daemon's own Playwright MCP client: JSON-RPC over a
**stdio subprocess**. A stdio subprocess is used instead of exposing the
Playwright MCP server over HTTP means that only the parent process (the daemon)
can connect to it. Nothing else can do so.

The shape follows `rail/client.py` — typed errors, timeouts as named constants,
and **no internal retry** (a lane backs off; a hot retry against a marketplace
is the anti-automation tell). The single exception is a tab that reports modal
state, which is recovered from rather than retried — see below.

Errors are three kinds because the responses differ:

| error | means | response |
| --- | --- | --- |
| `BrowserUnavailable` | the layer cannot run at all — no `node`/`npx`, or the server died at startup | skip the browser lanes, one needs-me notice, daemon runs on |
| `BrowserTransportError` | the server was there and the exchange failed — exited mid-call, timed out, unparseable frame | counted as a failed read / a send that did not happen |
| `BrowserToolError` | the tool ran and failed — a selector matched nothing, a navigation was refused | the browser is healthy; the action is not |

`ensure_available(command)` is a `shutil.which` check the factory runs *before*
spawning, so absence is reported as absence with an install hint, rather than
surfacing later as a failed read or a send that reserved pacing for nothing.

Notable client behaviors:

- **One tab, held by identity, not index.** `ensure_tab` opens the client's own
  tab once; because the daemon owns the server process exclusively, that tab stays
  the one its calls act on. Nothing ever selects a tab by index or guesses one by
  host — indices renumber whenever any tab opens or closes.
- **That handle is a claim, not a guarantee, and modal state is how it breaks.**
  The server names no tab in a call: every tool acts on whatever it currently
  considers the current tab, and it re-points that silently when a tab closes.
  So a tab of ours that the seller closes hands our calls to whatever tab is
  left — including one a pass opened. That matters because Playwright MCP
  refuses every page-level tool (`browser_evaluate`, `browser_snapshot`,
  `browser_click`) while the tab it is pointed at carries **modal state**: an
  open dialog or file chooser. The state is per tab, in the server's own memory,
  and only the tool that owns it clears it — and a server attached over CDP
  wraps *every* tab, so it records the same dialogs and file choosers as any
  other client on that Chrome and never clears them. A file chooser a publish
  pass opened and consumed therefore leaves our view of that tab refusing every
  read, for good: the state outlives the page, so navigating does not clear it,
  and the tool that would clear it must never be called on a flow that is not
  ours. `call_tool` recovers by giving the tab up, opening a fresh one (which
  cannot carry any), sending it back to the page the caller had navigated to,
  and retrying the call **once**. Left unrecovered this is a market reporting
  itself blind every tick until the daemon restarts, for a reason that has
  nothing to do with the market. Note also `browser_navigate` and `browser_tabs`
  are *not* modal-gated, so a poisoned tab still navigates — the reads are what
  fail.
- **`ensure_frontmost` before certain key events.** Chrome routes certain key
  events (like Enter) only to a visible renderer, and a tab is visible only
  when it is its window's active tab. Ergo, a tab must be active for it to
  receive these key events. This only applies to key events like Enter. The
  consequence of `ensure_frontmost` is that the browser will steal focus from
  the user.
- **`evaluate` runs JS on a page, and should never set a value.** With a target
  it is a locate-and-read. The text a buyer sees is should always be typed as
  real input.

## One Chrome, three actors

The read lane, the reply sink, and a browser-driving pass all reach the same tab,
and the first two genuinely overlap (a reply can be sent while the lane is
mid-read). Two mechanisms keep that safe:

- **A re-entrant mutex on the client**, taken for whole *operations* rather than
  single calls: `with client.exclusive()` wraps navigate-then-read and
  locate-then-type-then-verify, so no other actor can navigate away mid-sequence.
- **The lane yields entirely** while a browser-touching pass is queued or
  running (`browser_pass_running`). A rail publish is not a reason to yield; a
  browser publish holds the tab for minutes where the lane holds it for seconds.
  Worst case the lane notices a buyer message one tick late.

## Market adapters

`browser/markets/` contains market-specific adapters. Adapters implement the `MarketAdapter` interface. A new market is a new module plus a registry entry.

| field | what it carries |
| --- | --- |
| `conversations_list_js` | which conversations exist → `{conversations: [...]}` or `{error: …}` |
| `conversation_tail_js` | the open conversation's trailing bubbles, or `null` to abstain |
| `login_js` | `{state: logged_in \| logged_out \| unknown}` |
| `chat_message_submit_js` | **how a message is committed** — empty means a real key press |
| `listing_id_pattern` | where a listing's id sits in a permalink, one regex group |
| `composer` | shipped selector defaults, by step |
| `publish_skill` | the skill holding this market's publish recipe |
| `system_handles` | rows an inbox read must never treat as a buyer |

`chat_message_submit_js` is per-market decision rather than a per-market fact.
Empty is a safe default; submitting falls back to a real 'Enter' key event,
indistinguishable from a human, but requires the browser to steal focus from
the user (see `ensure_foremost`). This field can be used to trigger a synthetic
key event, which has the tradeoff of carrying a `isTrusted=false` signal, which
the site can use to detect bot activity. For marketplaces that expose a 'send'
UI element, prefer clicking on it to submit, as this does not require browser
focus.

The JS artifacts are the layer's only DOM knowledge. They should be made as
stable as possible. Marketplaces that ship with hashed CSS classes (e.g.
Carousell), should have elements located by more stable identifiers instead.
This may involve: role, href shape, element hierarchy, geometry, and so on.
identifiers instead.

### Registry vs. adapter

`data/marketplaces.json` says a market is `connector.type == "browser"`;
`browser_markets()` returns the active ones in registry order. Several entries
qualify today (Facebook, Mercari, OfferUp, Poshmark, Craigslist) and **only
Carousell has an adapter** — the lane skips a registry entry with no adapter, so
listing a market in the registry does not make it read. Page URLs come from the
registry's `urls` templates and the region→host `domains` map, never from a guess:
an unrecorded template resolves to `None` and the caller reports that.

Whether a market can be *published* to is two facts with two homes:

- **What we can drive is code.** `supported_markets()` reads the adapter registry
  and the recipe pointer — never a flag in the JSON, which could say yes while the
  adapter says no.
- **Where a market operates is data.** The `domains` map, which every URL already
  rests on. A map is **exhaustive**: a region absent from it is one the market does
  not serve, so `resolve_domain` answers `None` — no Carousell for a US seller
  (seven regional sites, no US one), and an MY seller with no code change.

`publishable_markets(region)` crosses the two, and gates the setting, both enqueue
doors and the fan-out alike.

Adapter-less entries still earn their place: `hosts.build_allowlist` reads every
entry's hosts, which is what stops the scam scanner flagging a legitimate eBay or
Mercari link.

### Adapter: Carousell

- **The conversation list is Carousell's own JSON API**, fetched from the page so
  the session cookie rides along. The inbox DOM cannot supply it: its rows are
  `div[role="button"]` with hashed classes and carry no link, id or data
  attribute, so a conversation there has no addressable identity at all. The API
  also *fails with a status code*, where a DOM read that finds nothing looks
  exactly like an empty inbox. Fetching it marks nothing read.
- **The thread id is `legacy_offer_id`, not `id`.** `id` is a 32-bit integer
  server-side and has wrapped, so a new conversation reports a negative one —
  which in the chat URL is a different conversation.
- **Message history is DOM work**, because chat lives in a separate service. The
  reader scopes to the single scrollable pane, then keeps only *inline rounded*
  containers: the header's "Online 11 days ago", profile cards, system notices,
  and Carousell's quick-reply suggestion chips are all block/flex with square
  corners. A chip is indistinguishable from a buyer message by text alone, and
  recording one would have the agent answering itself.
- **Direction is geometry.** An outbound bubble hugs the right edge, inbound the
  left; anything roughly centred is reported `center` so the caller ignores it.
- **The login probe is three-state and never guesses `logged_out`** — a false
  `logged_out` tells a signed-in seller to re-authenticate and stops their market.
  An auth-gated control (inbox / sell) proves `logged_in`; a login control with no
  such marker proves `logged_out`; anything else is `unknown`.
- **There is no send-button selector**, because there is nothing addressable to
  click: the send icon's ancestors are undecorated elements with no role, no
  label and no cursor change, while the message box handles Enter itself.

## The read lane

`browser/inbox.py`, on the `inbox_read` scheduler task. One tick asks each
browser market which conversations exist, opens the ones that look like they
moved, and reconciles each tail against the rows already stored — a navigate and
one JS evaluate per thread, **no model turns at all**. That is what lets the reply
pass above it stay browser-free: by the time it runs, what the buyer said is
already state.

Per market, per tick: navigate the inbox → login probe → conversation list →
for each row, adopt or match a thread, skip or open it, reconcile → one
`browser.read` event.

**The cadence follows the conversations.** The registered interval
(`inbox_read_interval_sec`) is the idle one; while any active sell thread has a
message inside `inbox_fast_window_sec` — in *either* direction, since the buyer
answering our reply is the commonest warm case — the lane reads every
`inbox_fast_interval_sec` instead, then decays back on its own. It is one
cadence for the whole lane rather than per thread: the conversation list is a
single API call carrying every thread's preview, so one read learns about all of
them and no thread's news is reachable without it. Quiet hours hold the slow
cadence, because the pacing gate refuses sends inside the window anyway. None of
this raises the reply rate — every send still reserves through that gate.

**Three rules keep it honest:**

1. *A market that cannot be seen must never look like a market with no news.*
   Failed reads are counted per market; `browser_blind_after` consecutive
   failures raise one needs-me notice. A conversation whose message list could
   not be found counts as blindness too — it would otherwise pass for "this buyer
   said nothing new", which is how a buyer gets stranded.
2. *The skip gate is an optimization, never a correctness input.* A thread whose
   last stored message still matches the list's preview is left closed — but one
   read per `inbox_full_sweep_interval_sec` opens every active thread regardless,
   so a wrong preview match costs one sweep interval of latency and nothing more.
   Anything unread, anything whose last message we don't hold, and anything never
   read is always opened. The sweep is paced by wall clock rather than by a count
   of reads, so it does not get more frequent when the cadence below speeds up —
   opening a thread marks it read on the marketplace. The first read of a market
   after a restart sweeps.
3. *Reading never advances the reply cursor.* Only a committed reply does, so a
   crash between seeing a message and answering it leaves the buyer eligible
   rather than silently handled.

**Adoption.** A buyer writing about one of our listings for the first time gets a
thread only if three things hold: they approached us (`offer_type == received`,
not an offer we made), the conversation names a listing we recognise by id, and we
know their handle. Anything less is left alone and emits `browser.unmatched` with
*which* check failed (`we_offered`, `no_handle`, `unknown_listing`, `two_items`) —
that event is what answers "why is nobody answering this buyer", and one label for
all of them would make the ordinary case (a listing the seller made outside the
agent) read like the alarming one (two items claiming the same listing).

**Scam pre-scan.** Every inbound row is scanned by the deterministic offline
engine as it is written, so the verdict is on the row before any model sees the
text.

Three notices, each queued at most once per condition and cleared on recovery:

| condition | notice |
| --- | --- |
| `browser_blind_after` failed reads on a market | can't read your `<market>` inbox — check Chrome is running and logged in |
| the login probe says `logged_out` | that market's session is logged out; reading stopped, and the notice names `selly-agent connect <market>` — the daemon opens the market and re-probes |
| `BrowserUnavailable` | the browser can't be driven at all; browser markets paused, the rail unaffected |

The reply lane (`reply_lane`, every 10s) is a sibling: it claims every waiting
thread into **one** coalesced reply pass, refuses to enqueue a second while one is
in flight, and auto-refires nothing — eligibility comes from the rows, so a failed
pass's threads are simply picked up next tick.

## Reconcile

`browser/reconcile.py` — pure functions, no I/O. **The rule is reconcile, not
infer**: the tail is read as ground truth and compared against the stored rows,
and whatever is not stored is new. No memo of what a previous read rendered is
kept anywhere; the state that decides is the state that persists.

- **Message ids are derived from content**, because the chat DOM exposes none:
  `<direction>|<sha256 prefix>|<occurrence>`. Occurrence numbering, counted from
  the stored copies, keeps ids unique across repeats and stable across reads.
- **The tail is aligned as a trailing window.** The longest suffix of the stored
  rows that matches the tail's opening is the region both sides agree on; only
  what follows is new. Counting copies of each text instead would swallow a
  repeat whose earlier copy has scrolled out of the window — the stored count
  exceeds anything an 8-bubble tail can still show, so a buyer's new "ok" would
  read as already handled.
- **Alignment is truncation-tolerant.** A reader may cap how much of a bubble it
  returns, so a long stored reply must still match its cut-short read-back;
  otherwise the bubble records as an outbound message we never wrote — a phantom
  manual seller reply, which silences the thread. Texts under 200 normalized
  characters must match exactly: "ok" opening "ok, deal" is a coincidence, not a
  truncation.
- **Alignment is agnostic about who wrote a row**, so our own committed replies
  and the seller's manual ones reconcile the same way — already-recorded outbound
  text, matched and not recorded twice.
- **`classify_tail` drops what nobody said**: separator rows (times, "Yesterday")
  and anything centred, which is a system banner or an offer widget. Keeping one
  would record it as a message and, worse, let it stand as "someone answered".
- **`preview_matches`** decides only whether to *skip* opening a thread, matching
  an inbox row's truncated preview against a stored message. Its wrong answers
  cost latency, never a stranded buyer, because the full sweep backstops it.

## The send

`browser/sink.py` fills the `ReplySink` seam `send_reply` sends through. One call
does the whole bracket: navigate the recorded thread URL → (bring the tab forward,
if this market sends with a real key) → locate the composer → fill it in one go
→ commit → stamp the intent → confirm by reading our own words back off the page.

The text is filled whole rather than typed character by character, so a reply
containing a newline cannot commit part-way through itself and send half a
message. Verification is strict: "no error from the key press" is not success — a
refused validation, a composer that silently cleared, or a chat that ignored the
key because it thought the box was empty all look like success from outside. Only
our own words in an outbound bubble count.

**The two failure shapes are treated oppositely, because the safe response to
each is the opposite:**

| | nothing was sent | sent, unconfirmed |
| --- | --- | --- |
| how it happens | composer not located, page refused the commit, browser error before the commit | the commit was accepted and the read-back failed or found nothing |
| intent status | stays `pending` | `sent_unverified` |
| the sink raises | `SendNotAttempted` | `SendUnverified` |
| `send_reply` returns | `send_failed` | `send_unverified` |
| what happens next | safe to retry | **never re-driven** |

Everything before the commit fails closed, so "nothing was sent" is a guarantee
and not a hope. Past the commit nothing may retry and nothing may claim the send
did not happen — because the one thing worse than an unconfirmed message is the
same message twice.

An unconfirmed send is then handed off, not resolved in code:

1. While it is open, `reserve_reply` refuses any fresh send on that thread with
   `unverified_open` — no caller can talk past it, and no second intent or pacing
   row is minted.
2. The `stale_intent_sweep` task folds the intent as `unconfirmed` past its grace
   window (600s, held well above the pacing delay ceiling so a merely-jittered
   send can never look like a stall) and opens an escalation. The thread becomes
   `escalated`, which is the gate from then on.
3. Only the seller can settle it, by looking at the real chat. If the message is
   there: resolve the escalation and reactivate the thread. If it is not: resolve
   and reactivate **first** — sends are refused while a thread is escalated — then
   send again. The framing lives in the `seller-comms` skill.

## Selectors and the heal cache

Selectors ship as code, because a fresh install should not pay a vision
round-trip to find a message box that was known-good at release. The `ui_cache`
table sits **over** those defaults as a heal overlay: when a marketplace moves a
control, whatever re-found it is recorded and used from then on, and a later
release refreshes the defaults underneath. So self-healing never waits on a
release, and a release never overwrites what an install has learned.

- **Order is cache-then-shipped.** A stale row is skipped rather than tried, and
  a cache row that misses is counted *even when the shipped default then works* —
  otherwise a heal gone bad is probed on every send forever. Three failures retire
  it, so the row stops costing anything with nobody invalidating it by hand.
- **Stale is a miss, never "act anyway."** A row is stale when it has failed
  three times, carries no page-URL guard, or has gone 30 days unverified. Every
  stale answer degrades to the slow path exactly as a miss would.
- **Resolving is locate-only and must match exactly one visible element on the
  right page.** None means absent; several means acting would be a guess — made
  once per send, silently, on the account-sensitive path. A page guard is
  mandatory: recording a row without one is refused, since it could never be
  trusted.
- **The table holds locating strings and timestamps only** — never a value, a
  price, or an address.

## The fan-out

`crosslist.py`, on the `crosslist_lane` scheduler task — what makes "list this" mean
"list where the seller sells". The publish itself is the pass
[the tool surface](tool-surface-and-passes.md) describes; this lane decides when one
is queued, and tells the seller how it went.

The seller opts in per market with `crosslist_markets`: empty by default,
approval-gated (enabling one makes the agent post publicly as them), and limited to
what `publishable_markets(region)` allows.

Per tick: report settled publishes → queue at most one more.

**Eligibility is a query, not a recipe step.** An item qualifies when it is live on
carousell.ai, missing from an enabled market, not sold, and never attempted there.
Reading that off rows is what buys:

- **rail-first as a precondition** — no carousell.ai URL, no fan-out, so it cannot
  happen out of order;
- **idempotence** — `record_published_listing_url` writing the URL stops the pair
  qualifying;
- **backfill** — items listed before the setting existed qualify the moment it is
  turned on.

**One shot per item and market.** A settled pass ends automatic attempts for that pair
whatever its outcome — an attempt is minutes of browser work and a vision-priced token
bill. The cheap preconditions (Node installed, Chrome up) retry every tick and are
checked *before* queueing, so a condition that fixes itself never spends the attempt.

**Asking is what restarts it.** Plenty of failures say nothing about whether the next
attempt would work — the harness running out of credit is the one that prompted this —
and the one shot leaves the item stranded with no way back. `queue_marketplace_publish`
is that way back, on the channel and attended tiers, so the seller can ask for another go
from wherever the failure notice reached them. It queues what the lane would have queued
and skips only the one-shot: a seller who asks has chosen to spend the attempt. Every
other condition still holds, and one it adds — no second publish of an item already
listed on that marketplace or already under way, which is the mistake that would leave
two live listings on the seller's own account. Quiet hours do not apply; they hold
unprompted work, and this was prompted.

The lane holds off while paused, while another publish is queued or running (passes run
one at a time), and inside quiet hours — a publish is a visible burst on the seller's
real account. Quiet hours hold the *start* of work; nothing running is interrupted.

**The outcome is read off the rows, not off the pass.** A recorded URL becomes a success
notice with the link. Anything else — including a pass that exited clean having recorded
nothing, which leaves no listing anyone can find — becomes a failure notice naming the
item and the market, and inviting the seller to ask for another go. `passes.reported`
flips in the same transaction as
the notice, so a crash mid-sweep neither announces twice nor swallows. Publishes run by
hand carry no `crosslist` origin and are not reported.

**The push closes the loop the other way.** The lane's third phase writes each item's
browser-listing URLs onto its carousell.ai listing (`external_urls`, which the listing
page renders to buyers as "Also available on"). A push is owed when the set derived
from the item's recorded listing URLs differs from what the rail last accepted — the
`crosslink_pushes` marker table, written only after the rail says yes — so steady state
costs nothing and backfill is just the first tick. Eligibility comes from where the
item actually is, never from `crosslist_markets`: an attended publish outside the
fan-out earns the link too. Failure is silent-retry — `crosslink.push_failed` and
eligible again next tick, no needs-me notice (an unlinked listing costs
discoverability, not a sale, and the seller cannot act on it). Unlike the enqueue
phase, the push is *not* held by quiet hours and takes no pacing reserve: it is one
API call on our own rail, not visible activity on the seller's marketplace account.

## Events

| kind | when |
| --- | --- |
| `crosslist.queued` | a fan-out publish was queued for an item and market |
| `crosslist.reported` | a settled fan-out publish was reported to the seller, with the URL and whether it worked |
| `crosslink.pushed` | an item's cross-listing URLs were written onto its rail listing, with the platforms in the set |
| `crosslink.push_failed` | a push the rail refused or never received; retried next tick |
| `browser.chrome_launched` | the daemon started Chrome because an acquisition needed it |
| `browser.read` | one market's tick: rows listed, threads opened, rows recorded, unreadable count, whether it was a full sweep |
| `browser.inbound` | one message folded into a durable row, with its scam verdict |
| `browser.thread_new` | a buyer's conversation adopted as a thread |
| `browser.unmatched` | a conversation deliberately not adopted, and which check failed |
| `browser.blind` | a failed read, with the running count and the reason |
| `browser.login` | the login probe answered `logged_out` |
| `browser.unavailable` | the browser cannot be driven at all |
| `browser.send` | a send's outcome: `sent`, `refused`, `unverified`, `browser_error` |
| `browser.heal` | a selector candidate that did not resolve, and where it came from |

## Configuration

| key | default | what it does |
| --- | --- | --- |
| `chrome_cdp_port` | `9222` | the warm Chrome's CDP port on loopback — in a container, the port the forwarder listens on *and* forwards to |
| `chrome_bin` | `null` | the Chrome executable to start; `null` means the OS default install path |
| `playwright_mcp_cmd` | `null` | override the server command; `null` means `npx --yes @playwright/mcp@<pinned version>` against the CDP endpoint. The pin lives in `browser/client.py` (`MCP_VERSION`) — bump it there |
| `inbox_read_interval_sec` | `300.0` | how often the read lane ticks when nothing is live — also the ceiling, since the dynamic cadence can only read sooner |
| `inbox_fast_interval_sec` | `60.0` | how often it ticks while a conversation is warm; must not exceed `inbox_read_interval_sec` |
| `inbox_fast_window_sec` | `300.0` | how long after the last message, in either direction, a conversation counts as warm |
| `crosslist_lane` interval | `30.0` (code) | how soon a seller hears a listing went up; not throughput — one publish is queued per tick at most |
| `inbox_full_sweep_interval_sec` | `1800.0` | how often one read opens every active thread; at or below the read interval, every read is a full sweep |
| `browser_blind_after` | `3` | consecutive failed reads before the needs-me notice |

`browser_blind_after` stays a count of reads rather than a duration, so the
needs-me notice arrives sooner while the lane is reading fast — which is when
going blind matters most.

Lane state (last full sweep per market, consecutive failures, which notices are
already queued) lives **in process** on purpose: a restart re-arming it errs
toward reading more rather than less.

## Adding a marketplace

1. **A registry entry** in `data/marketplaces.json`: `connector.type: "browser"`,
   `status: "active"`, the `domains` map for its regions, and `urls` templates for
   at least `inbox` and `thread`.
1b. Nothing else: `selly-agent connect <market>` picks the new id up from the
   adapter registry, and so does the healthcheck's per-market login line.
2. **An adapter module** under `browser/markets/`, exporting the JS artifacts,
   the composer defaults, the listing-id pattern and its system handles — then one
   line in that package's `_ADAPTERS` registry.
3. **Decide the submit mechanism.** Leave `chat_message_submit_js` empty unless
   someone has decided that market's account can afford a page-dispatched
   keystroke.
4. **A publish recipe skill**, if the market should be publishable, pointed at by
   the registry's `listing_flow`. Steps 2 and 4 together are what make the market
   selectable in `crosslist_markets` — there is no separate switch to remember.

Nothing else in the layer changes: the read lane, reconcile, the sink and the
selector cache are all written against the protocol.
