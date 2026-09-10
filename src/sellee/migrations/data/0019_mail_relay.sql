-- Buyer conversations that arrive as email, and the state a restart must not lose.
--
-- Craigslist has no messaging. A buyer's only route to a seller is an anonymised relay address, so
-- the conversation lives in the seller's own mailbox and this is the state that makes it a thread
-- the rest of the agent can work: `threads`, the reply lane, the intent lifecycle and the scam
-- pre-scan are all reused untouched above it.
--
-- Two tables, and each exists because the alternative is a specific silent failure.
--
-- `mail_relay_threads` maps a conversation to its thread, and holds the fact that ends it. Keyed on
-- the mail provider's own thread id, because **the relay address cannot identify anything.**
-- Measured on one live posting (id 7963439877) on 2026-09-10, five distinct relay addresses:
-- the one a buyer's message arrived on, one revealed to a logged-out viewer, one revealed to the
-- seller's own session, and two from consecutive reloads of the same page seconds apart. Craigslist
-- mints a fresh address per *view* — an anti-scraping design. So the hex is not a posting id, not a
-- conversation id and not a viewer id, and keying a thread on it would group buyers arbitrarily.
--
-- The address is kept beside the key for exactly one job: it is where a reply goes, and only the
-- `reply.` address a buyer's own message arrived from will reach that buyer. It expires, and when it
-- does **the conversation is unreachable** — re-reading the posting yields a new `sale.` address,
-- which is what a buyer writes to in order to reach the seller, so sending there mails the seller
-- their own reply. Which posting a conversation is about comes from the permalink in the message
-- body instead, because that does not rotate.
--
-- `state` is what stops us writing into a dead conversation. A relay thread outlives its listing —
-- craigslist keeps one alive for up to four months after the posting is deleted — and it can end
-- three ways that all look like silence from outside: the buyer opts out, the relay starts
-- bouncing, or four months pass. Held durably because a restart that forgot would resume sending
-- into an address that has stopped accepting, and read the bounces as new buyer messages.
--
-- A retired address is a fourth ending, and the one we cause rather than the buyer: it looks like an
-- ordinary bounce and cannot be recovered from, because nothing craigslist will hand back reaches
-- that buyer again. It is held distinctly from `opted_out` because what the seller is told differs —
-- "they asked not to be contacted" versus "craigslist retired the address before we answered" — and
-- the second is a latency problem of ours, not a choice of theirs.
--
-- `mail_relay_seen` is idempotence. The transport reads a scoped view of a mailbox, and a read has
-- no cursor it can trust — a message stays in the view, and a provider may reorder or re-render it.
-- Without a record of what has already been folded, every read would journal the same buyer message
-- again: `reconcile` aligns a tail against stored rows and would find a new one each time, so the
-- buyer would look like they had said the same thing repeatedly and the agent would answer it
-- repeatedly.

CREATE TABLE IF NOT EXISTS mail_relay_threads (
    -- The mail provider's own id for the conversation. Opaque to us, and the only
    -- per-conversation fact a scoped mailbox read has.
    provider_thread_id TEXT PRIMARY KEY,
    -- Where the next reply is sent, as of the last message that arrived. **Volatile**: craigslist
    -- rotates a posting's relay address, and a stored one bounces with "get a current reply email
    -- address" (measured). So this is a cache of the latest, never an identity, and a stale-address
    -- bounce means re-read the posting rather than close the conversation.
    relay_address TEXT,
    -- `<market>:<provider thread id>`, as `store.create_thread` requires. The market is the
    -- marketplace the buyer is on — craigslist — never the mailbox we read it through.
    thread_id TEXT NOT NULL,
    market TEXT NOT NULL,
    -- Which posting the conversation is about, as the permalink craigslist puts in the message
    -- body. The address's hex cannot answer this: it rotates, so two buyers writing in different
    -- windows carry different ones. The URL does not rotate, and it is what a stored listing URL
    -- can be matched against.
    posting_url TEXT,
    -- open | opted_out | bouncing | expired. Only `open` may be sent to.
    state TEXT NOT NULL DEFAULT 'open',
    -- Why it stopped, for a notice that can say something more useful than "it failed".
    closed_reason TEXT,
    -- When the buyer was invited to forward the thread to the seller's own +tagged address, or
    -- NULL if they have not been. Durable because the invitation goes out with the *first* reply
    -- and then stops: repeated every message it reads as a bot and buries the answer the buyer
    -- asked for, and a restart that forgot would repeat it. See `sellee.mail.handoff`.
    --
    -- This is the escape from the rotation above. A conversation that stays on the relay eventually
    -- becomes unreachable with no warning; one that has moved to the seller's own address is
    -- ordinary email, with no expiry and honest bounces.
    handoff_sent_ts REAL,
    -- When the conversation first arrived on the seller's own handoff address instead of through
    -- craigslist's relay, or NULL while it is still on the relay. **This decides whether a payment
    -- link may be sent.** On the relay leg a link is refused: link-bearing relay mail is commonly
    -- dropped, and craigslist's own advice tells buyers never to pay through a seller's link. On
    -- the direct leg craigslist is not in the path at all, so the close is an ordinary checkout
    -- link like every other market.
    --
    -- Durable because a restart that forgot would send a payment link into the relay, where it
    -- vanishes with no bounce and no error — the seller believes the buyer was asked to pay and the
    -- buyer never saw anything.
    off_relay_ts REAL,
    first_seen_ts REAL NOT NULL,
    last_seen_ts REAL NOT NULL
);

-- The join a message does on arrival: posting URL -> which conversations are about that item.
CREATE INDEX IF NOT EXISTS idx_mail_relay_threads_posting
    ON mail_relay_threads (posting_url);

-- Answering "is this thread still sendable" without scanning.
CREATE INDEX IF NOT EXISTS idx_mail_relay_threads_state
    ON mail_relay_threads (state);

CREATE TABLE IF NOT EXISTS mail_relay_seen (
    -- The provider's own id for the message. Opaque to us and never parsed: the only question
    -- asked of it is whether it has been seen before.
    provider_message_id TEXT PRIMARY KEY,
    provider_thread_id TEXT,
    seen_ts REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mail_relay_seen_conversation
    ON mail_relay_seen (provider_thread_id);

-- The mailbox itself: whether the seller is actually signed in to it, and what we may read there.
--
-- Before this, "the mailbox is connected" was `craigslist_mail_view != ""` — an https string the
-- seller typed at a shell. Never navigated, never probed, never re-checked. So a seller who set it
-- and was signed out got told their replies were being sent, and a seller who was signed in but had
-- not set it got told to connect a mailbox they had already connected. One row, probed.
--
-- `provider` is checked rather than assumed. Craigslist's mail is only driven for Gmail, and a
-- webmail this transport has never been measured against must be refused by name — a mailbox that
-- reports itself permanently unreadable is worse than one that says "not this provider yet".
--
-- `handoff_address` lives here rather than only in settings because it is half of a *verified*
-- pair: the address is useless unless mail sent to it lands in the view we read, and `verified_ts`
-- is the record that a real message made that trip. Craigslist mints a relay address per view and
-- an expired one is unrecoverable, so this address is the only durable way back to a buyer — and
-- since a payment link may only be sent once a conversation has moved onto it, it is also the only
-- way a Craigslist sale closes.
CREATE TABLE IF NOT EXISTS mail_transport (
    -- One row per marketplace whose buyers arrive as mail. Keyed on the market, not the provider,
    -- because it is the market's promise this backs.
    market TEXT PRIMARY KEY,
    -- What the probe found, not what we hoped for: 'gmail' | 'unknown'.
    provider TEXT NOT NULL DEFAULT '',
    -- The scoped view actually navigated and read successfully. Written only after a probe.
    view TEXT NOT NULL DEFAULT '',
    -- The +tagged address buyers are asked to forward to. Derived from the account's own address,
    -- shown to the seller rather than typed by them.
    handoff_address TEXT NOT NULL DEFAULT '',
    -- 1 only while the last probe saw a signed-in mailbox. A dead session must read as dead rather
    -- than as a Gmail DOM change, which is the same distinction the market login probe makes.
    signed_in INTEGER NOT NULL DEFAULT 0,
    -- When the scoped view was confirmed to query this address coherently — Gmail answering the
    -- `deliveredto:` clause with results or an honest "no messages matched", rather than an error.
    --
    -- That is a narrower claim than "mail reaches this address", and deliberately so. An earlier
    -- design sent a synthetic self-test at connect time to prove delivery; a synthetic test can
    -- pass while a real forward fails, and it needs a compose flow this transport does not have.
    -- **Delivery proves itself instead**: `mail_relay_threads.off_relay_ts` can only be set by a
    -- real message arriving at this address, so the gate that actually depends on delivery — the
    -- close, which may only send a payment link on the direct leg — rests on unfakeable evidence.
    --
    -- What this column still catches is the failure that did happen: a malformed clause. A literal
    -- `+` in a Gmail hash query is a space, so `deliveredto:you+cl@x.com` silently became
    -- `deliveredto:you cl@x.com` and the whole view answered "no messages matched" with a buyer's
    -- thread sitting in it — indistinguishable from an empty mailbox.
    verified_ts REAL,
    last_probe_ts REAL,
    updated_ts REAL NOT NULL
);
