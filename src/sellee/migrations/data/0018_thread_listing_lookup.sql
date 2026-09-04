-- What a conversation is about, remembered — including when the answer is "nothing of ours".
--
-- Carousell's inbox rows carry the listing id. Facebook's do not: it names the listing only on the
-- banner INSIDE the conversation, so the read lane navigates into each one to ask. That answer was
-- then thrown away unless it matched, because the only place a resolved id was kept was the thread
-- row, and a thread is created only when the match succeeds.
--
-- So every conversation about a listing we do not manage — declined, sold outside the agent, or
-- failed adoption as `no longer for sale` — was re-opened every five minutes, forever, to
-- re-derive an answer that cannot change. Measured on a live install on 2026-09-01: 25 Facebook
-- conversations at ~5.5 seconds each, about two and a half minutes of continuous page loading per
-- sweep, indefinitely.
--
-- `product_id` empty means "we looked and there was nothing", which is the whole point: a negative
-- is as worth remembering as a positive, and is the case that was costing the most.
--
-- `row_key` is which listing the row said it was about when we looked — its title, and only that.
-- A change to it re-opens the conversation, which bounds a positive whose conversation moved to a
-- different listing after a relist. It once carried the last message too, so that any new message
-- re-opened the conversation; that made the answer self-heal in both directions, but it also meant
-- a chatting buyer on a listing we do not manage cost a page load per sweep for an answer that
-- cannot change — the exact cost this table exists to remove.
--
-- A negative that should now match is bounded instead by the write that changes it: every writer
-- that gives an item this market's URL forgets the market's rows in its own transaction, via
-- `_forget_thread_listings_in_txn`. Nothing else can turn "none of ours" into a match.
--
-- Growth is decided rather than left to chance: one row per conversation ever seen, never pruned,
-- tens of bytes each. Bounded by how many people actually message a seller — tens to low thousands
-- over an install's life — so it is accepted as-is. If it ever needs a bound, delete by `looked_ts`
-- beside the survey-decision expiry that already exists.

CREATE TABLE thread_listing_lookups (
    thread_id   TEXT PRIMARY KEY,
    market      TEXT NOT NULL,
    product_id  TEXT NOT NULL,
    row_key     TEXT NOT NULL,
    looked_ts   REAL NOT NULL
);

CREATE INDEX thread_listing_lookups_market ON thread_listing_lookups (market);
