# Craigslist — review of the carousell.ai relay, and what's on this branch

Written for the engineer continuing the Craigslist connector on top of
`carousell-ai-email-relay/12-sellee-relay-fixes-from-the-break-suite`.

## Verdict on the relay: it's the right design, and it does remove inbox monitoring

Reviewed `rail/inbox.py`, `rail/sink.py`, `store/relay.py`, `0020_relay_cursor.sql` and the fake
MCP. It is cleaner than the approach it replaces and the correctness details are right:

- rows deduped on bazaar's message id, **cursor stored only after they commit** — a crash between
  the two costs a refetch, never a doubled message;
- `relay_rereads` keeps an unsettled thread read every tick, because bazaar only re-lists on a new
  message or a block;
- the send is idempotent on `client_message_id`, minted from the intent before the first call, so
  any failure short of a refusal retries under the same id and cannot double-send;
- `is_refusal` draws the line that matters — a refusal stored nothing (drop the intent), anything
  else may have stored it (leave unverified, settle from a later read). 408/429 and 5xx-shaped
  transport text are correctly on the retry side.

Tests are green: **3421 passed**. The 3 `test_logs` failures are the known environment issue (a
live daemon holding port 7355), not this branch.

**It replaces the mailbox-reading design entirely.** The superseded approach drove the seller's
Gmail in a browser tab to read Craigslist relay mail. That carried a privacy surface (no provider
offers a per-sender read scope, so the narrowest grant is the whole mailbox), a Gmail-only
limitation, and a fragile DOM dependency. Routing through carousell.ai removes all three. Good call.

## What this branch adds, and what it deliberately leaves out

Purely additive — **no behaviour change**. `craigslist` is not registered in `_ADAPTERS` and not in
`marketplaces.json`, so nothing is live yet; the artifacts are exercised directly by their tests.

| added | what it is |
| --- | --- |
| `browser/markets/craigslist.py` | login probe, `my_listings`, `listing_detail`, listing-id pattern — all captured from the live site |
| `craigslist_areas.py` + `data/craigslist_areas.json` | area lookup, plus two gates: `SERVED_STATES` (legal envelope, `{"CA"}`) and `CRAIGSLIST_AREAS` (GTM rollout, `{"sfo"}`), with `CRAIGSLIST_AREAS ⊆ SERVED_STATES` asserted at import |
| `skills/listing-flow-craigslist.md` | the publish recipe |
| 3 test files, 50 tests | artifact behaviour, area gates, listing-id agreement |

**Left out on purpose** — superseded by the relay, and present in full at
`feat/craigslist-primitives` (`105bee8`) if any of it is wanted:

`mail/` (Gmail DOM transport, ~2.7k lines) · `store/mail.py` · `connectables.py` (a two-sign-in
seam — with the relay there is no second sign-in) · `reply_sink.py` (a market-aware composite —
`rail/sink.py` covers it) · migrations `0019_mail_relay` / `0020_connect_targets` (**note:** that
`0020` collides with your `0020_relay_cursor`; it is in the dropped half, so it is moot unless you
revive it).

## Measured facts about Craigslist, so you don't have to re-derive them

All captured live on 2026-09-10 against a real posting and a real signed-in account.

**Relay addresses are minted per *view*, not per posting or conversation.** Five distinct addresses
were observed for one posting (id 7963439877), two of them from consecutive page reloads seconds
apart. The hex carries no identity — not the posting, not the buyer. Anything keyed on it is wrong.

**A posting has two address families.** `<hex>@sale.craigslist.org` is what a buyer writes to;
`<hex>@reply.craigslist.org` is the From on the message as it reaches the seller. Both resolve the
same MX (`mxia.craigslist.org`).

**Craigslist passes contact details through unaltered** — their own help page says so, and a reply
containing an email address was treated no differently from one without.

**Postings are driveable.** The composer at `post.craigslist.org/c/<area>` has conditional stages
(`area → subarea → hood → type → cat → edit → geoverify → editimage → preview → finalize`), which is
why the publish path is a recipe rather than a fixed sequence. One real publish was completed
end to end. `geoverify` is mandatory and needs a **postal code**, which no item record carries.

**The account page's real shape** is `tr.posting-row` with `td.status`, `td.postingID`,
`td.title a[href]`, `td.expdate`, `td.posteddate`, `td.areacat`. Two traps, both already handled in
`craigslist.py`: an empty account states "no postings" in words (read as `{error}` it burns all five
survey attempts and abandons the market within minutes of connecting), and the row link text is
`"<title> - $<price>"`, which lands in the item title unless stripped.

**Listing ids live in two spaces.** The canonical URL carries a base62 token; `td.postingID` carries
the numeric post id. They must agree with whatever the publish path stored, or re-adoption creates a
second item per listing.

## The one thing that blocks Craigslist on the relay

### 1. There is no per-listing address to put in the ad

A Craigslist posting needs a contact email **at posting time**, before any buyer exists. The only
address in the MCP contract is `thread_email` (`<thread_id>@reply.carousell.ai`), which is
per-thread, exists only after a thread does, and is consumed as `counterpart_handle`
(`rail/inbox.py:113`). `create_listing` returns `{listing_id, url}` and nothing else.

So the piece to add is a **per-listing relay address** exposed through MCP — something the publish
recipe can read and type into the ad's contact field, with bazaar binding inbound mail on it to that
`listing_id`. If bazaar already mints one, it just needs surfacing on `create_listing`/`get_listing`.

### 2. ⚠️ Verify that Craigslist actually forwards a reply *before* building on it

This is the finding I'd most want checked. Measured on the posting above:

| direction | address | result |
| --- | --- | --- |
| buyer → seller | `<hex>@sale` | **worked, 3 of 3** |
| seller → buyer | `<hex>@reply` | accepted, no bounce, **never arrived** (2×) |
| seller → buyer | `<hex>@sale` | accepted, no bounce, **never arrived** |
| seller → buyer | **hand-typed**, no automation in the path | accepted, no bounce, **never arrived** |
| seller → buyer | a *stale* hex | **bounced** `550 … get a current reply email address` |

The hand-typed control makes it a fact about Craigslist rather than about any code. The stale-hex
bounce rules out a blind catch-all: the relay has routing state and chose to accept and discard the
live sends. Nothing Craigslist documents predicts this.

**Why this may still be fine for the relay, and why it's cheap to check.** Those sends came from a
Google Workspace account (`@thecarousell.com`). carousell.ai sends from a domain it controls, with
its own SPF/DKIM/DMARC and sending reputation — materially different, and a plausible explanation
for the asymmetry. It is also **one posting on one account, created by automation on a young
account**, which is not enough to call it universal.

So: before wiring the publish path to a relay address, send one message from the carousell.ai relay
domain to a live `<hex>@reply.craigslist.org` and confirm a buyer receives it. If it arrives, the
whole loop closes. If it doesn't, Craigslist is publish-and-adopt only, buyers are passed to the
seller to answer, and the copy has to say so.

## Smaller notes

- **Threads arrive as `marketplaces.RAIL`.** A Craigslist-originated thread would be attributed to
  carousell.ai, and the summary carries no source field. Probably right — the item is on the rail
  anyway — but it decides what `connected_markets` gates and what the copy can say.
- **One attempt per item on Craigslist.** `PUBLISH_MAX_ATTEMPTS = 3` counts pass rows, and a publish
  that succeeded but stopped before recording a URL re-qualifies. On a market that punishes
  duplicates with silent ghosting, three attempts means three postings inside the 48-hour
  over-posting rule. The dropped branch has a per-market cap for this.
- **An expired post is never relisted.** `pending_pairs` skips a pair once a URL is recorded, which
  is correct where listings don't expire. A free Craigslist post dies in 7–45 days and the URL
  stays, so "listed everywhere" quietly stops being true. Clearing the URL isn't the fix — it joins
  buyer threads to items, and relay threads legitimately run 4 months past deletion.
- **Terms.** Craigslist's TOU prohibit automated posting by name, with a distribution clause that
  names the distributor and not only the operator. The posture taken was open-source-run-at-your-own
  -risk, disclosed in the connect copy. Worth a written opinion before this is a shipped feature.
