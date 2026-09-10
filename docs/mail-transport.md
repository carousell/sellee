# The mail transport

Craigslist has no messaging. A buyer's only route to a seller is an email, so answering Craigslist
buyers means reading the seller's **mailbox** — and that makes Craigslist the first market needing
**two sign-ins**: the site, then the mailbox its buyers email.

Everything above the transport is reused unchanged: the threads store, `reconcile`, the reply lane
and its coalesced pass, the intent lifecycle, the offline scam pre-scan. The mail layer's whole job
is to turn a scoped mailbox read into the same rows the browser inbox lane produces — and then to
pass the buyer's words to the seller, because craigslist's relay does not carry a reply back (see
below).

## What is where

| file | job |
| --- | --- |
| `connectables.py` | resolves a *connect target* — a marketplace or a marketplace's mailbox |
| `mail/relay.py` | which senders are in scope, what identifies a conversation, what a bounce means |
| `mail/gmail.py` | every Gmail artifact: login probe, list read, tail read, compose, send, verify |
| `mail/connect.py` | the mailbox sign-in's decisions and copy — provider check, handoff derivation |
| `mail/lane.py` | the read lane, and the half of the connect that needs a browser |
| `mail/transport.py` | `MailReplySink` — the send |
| `mail/outbound.py` | the one outbound boundary: a checkout link, on the relay leg |
| `mail/handoff.py` | the invitation that gets a buyer off the relay |
| `store/mail.py` | `mail_transport`, `mail_relay_threads`, `mail_relay_seen` |

## The finding that shaped this: the relay is one-way

Craigslist's relay carries a buyer's message to the seller and **has never been observed carrying a
reply back.** Measured on one live posting on 2026-09-10:

| direction | address | result |
| --- | --- | --- |
| buyer → seller | `<hex>@sale` | **worked, 3 of 3** |
| seller → buyer | `<hex>@reply` | accepted, no bounce, never arrived (2×) |
| seller → buyer | `<hex>@sale` | accepted, no bounce, never arrived (1×) |
| seller → buyer | **hand-typed**, no code in the path | accepted, no bounce, never arrived |
| seller → buyer | a *stale* hex | **bounced** `550 … get a current reply email address` |

The hand-typed control is what makes this a fact about craigslist rather than about this code. The
stale-hex bounce rules out a blind catch-all — the relay has routing state and chose to accept and
discard the live sends. And craigslist's own documentation predicts none of it: contact information
"passes through unaltered", threads "continue for up to 4 months", the relay-error page lists no
silent-drop case, and both relay domains resolve the same MX.

**So the promise is withdrawn rather than kept badly.** `market_adapters.READ_ONLY_BUYERS` holds the
flag and the evidence. What the transport still delivers is real: it reads the buyer, scam-scans the
message, joins it to the item, and passes the buyer's own words to the seller in chat — instead of
the seller watching an inbox. Answering is theirs.

Consequences, all mechanised rather than remembered:

- `answers_buyers("craigslist")` is `False`, so every seller-facing string says answering is theirs
  — mechanised through `can_answer_buyers`, so no amount of seller state can flip it back.
- `inbox.unanswerable_markets` keeps those threads out of the reply lane, so no model pass is spent
  composing a reply that cannot be sent.
- `MailReplySink` is kept and refuses. A transport that cannot deliver must say so, not accept.
- If a clean account is later observed replying successfully, delete the `READ_ONLY_BUYERS` entry
  and its surfaces waiver; the predicate flips everything else back on its own.

## Measured facts this design rests on

Each of these was observed, not reasoned, and each one killed an earlier design.

**Relay addresses are minted per view.** Five distinct addresses were observed for one posting
(id 7963439877), two of them from consecutive page reloads seconds apart. The hex identifies
nothing — not the posting, not the conversation, not the viewer. So a thread is keyed on the mail
provider's own thread id, and `relay.posting_hex` raises rather than returning a plausible answer.

**An expired relay address is unrecoverable.** Re-reading the posting yields a brand-new address
bound to nothing. (An earlier version of this line added "sending there mails the seller their own
reply" — that was inference stated as measurement, and when it was finally tested the message simply
vanished like every other outbound send.)

**Silent non-delivery is real.** A reply confirmed by Gmail ("Message sent", present in Sent), sent
to a retired relay address, produced **no bounce of any kind** and never arrived. So neither a send
confirmation nor the absence of a bounce is evidence a buyer was answered.

**The scope guarantee is a property of the tab.** Gmail keeps rendered list rows in the DOM across
hash navigations. In one tab: a first load of the scoped search was clean; after visiting `#inbox`
and navigating back, **34 of the seller's personal emails were in the DOM** of a search whose hash
was right and whose title said "Search results"; a fresh tab was clean again. Hence
`BrowserClient.fresh_tab`, and hence `relay.is_in_scope` running inside the page — it is what
actually held when the structural claim failed.

**Craigslist rewrites addresses, not names.** A buyer's message arrived carrying the display name
`jerry neo`. Normal disclosure is allowed by decision, so this is expected rather than a leak.

## The two legs, and why the handoff is built but cannot bootstrap

A Craigslist conversation has two legs with opposite properties:

| leg | route | links |
| --- | --- | --- |
| relay | buyer → `…@sale.craigslist.org` → seller | **refused** — relay mail commonly drops them, and craigslist tells buyers never to pay via a seller's link |
| direct | buyer → the seller's own address | **allowed** — ordinary email, craigslist not in the path |

The design was: the first reply invites the buyer to forward the thread to the seller's own address,
`off_relay_ts` is stamped when a message arrives there, and the close happens on the direct leg with
a normal `carousell_ai_create_checkout_link`.

**The relay finding breaks the bootstrap.** The invitation would have to travel the one path that
does not work, so nothing gets a conversation onto the direct leg by itself. Two things follow:

- The leg machinery is **kept and correct**, because a buyer can still reach the direct leg by
  writing to the seller's address on their own — from a phone number in the ad, or because the
  seller told them by hand. When that happens the lane stamps the leg and the close works.
- `_is_a_buyer_writing_direct` excludes the seller's *own* address, so the agent's own replies (now
  visible in the content-scoped view) cannot move a conversation to the direct leg by themselves —
  which would have put a payment link into the relay.

So the handoff is not dead code; it is a path with no automatic on-ramp. If a clean account is
observed replying through the relay, the on-ramp returns with the `READ_ONLY_BUYERS` entry.

## Why the mailbox is not a marketplace

`connected_markets` is the seller's marketplace opt-in, and `tools/reply.py` refuses a send unless
the thread's market is in it. A Gmail "market" would attribute Craigslist conversations to Gmail and
offer the seller a marketplace to list on with no listings. So the *target* of a sign-in is
generalised instead: `craigslist` and `craigslist-mail`, resolved by `connectables.resolve`, with
every market keeping its existing id so stored rows, callback tokens and CLI verbs keep working.

## The gate

`settings.publish_markets` refuses a mail market whose mailbox is not connected. Publishing to
Craigslist puts a public ad up with an email address on it; with no mailbox, every buyer who writes
gets silence, so the ad is worse than no ad.

Gating there is what makes the rule hold everywhere — every route to a live Craigslist post runs
through that list: the `queue_marketplace_publish` tool, `sellee pass run publish`, the crosslist
fan-out (whose `pending_pairs` backfills the whole catalogue the moment a market becomes
publishable), the MCP proxy, and the healthcheck's login probes.

`crosslist._explainable_markets` deliberately asks the *wider* question, because a gated market is
an invisible one and that lane's job is to say why nothing went up.

## What the seller is told, and when

Every seller-facing promise branches on `market_adapters.can_answer_buyers(market, store)` — the
code **and** this seller's state. Three predicates, and confusing them is a bug in either
direction:

- `answers_buyers_in_browser` — is there a conversation to open and a composer to type into. What
  the browser lane asks.
- `answers_buyers` — can this market's buyers be answered *at all*, by any transport. A capability.
- `can_answer_buyers` — capability **and** the mailbox is connected. What copy asks.

Mechanised on purpose: the alternative is a set of strings someone must remember to flip in the same
commit that wires the transport, and getting it wrong either promises replies that do not happen or
disclaims replies the agent is sending.

## Every way in

| door | notes |
| --- | --- |
| `./setup` / `sellee setup` | chains the mailbox after the site sign-in; shows what is already on before the picker |
| `sellee connect craigslist` | turns the market on **and** chains the mailbox |
| `sellee connect craigslist-mail` | just the mailbox — the common case, since a webmail session expires long before a marketplace one |
| `/connect` in chat | offers each mailbox directly after its market |
| `/sellee` → Connect | chains the mailbox and says so on the tap |
| approving a proposed `connected_markets` change | requests the sign-ins, as the button does |
| the mail lane itself | asks once for a missing mailbox — the only route to an install that predates this, since `sellee update` re-runs no setup phase |
| `sellee healthcheck` | a "marketplace mailboxes" line; never green when it could not be checked |

## Still open

- **Whether any account can reply through the relay.** One posting, one seller account, four
  failures including a hand-typed control. Enough to withdraw the promise, not enough to know
  whether it is universal or specific to a posting made by automation on a young account. A clean
  account would settle it.
- **Gmail only.** Another provider is refused by name at connect. The seam takes a second one.
- **Bounces are not read.** They arrive from `mailer-daemon`, not a craigslist domain, so the
  scoped view does not show them and the guard blocks them. `relay.bounce_kind` exists and has no
  caller; it needs a third, narrow view clause.
- **Listing liveness.** A free Craigslist post dies in 7–45 days and the recorded URL blocks
  re-qualification forever.
