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
-- `row_key` is what the row said when we looked — its title and last message, with the trailing
-- relative-time token stripped, since a ticking clock ("2m" becoming "1h") is not new information.
-- Any change to it re-opens the conversation, which is what keeps a stale answer bounded to a
-- single sweep in either direction: a negative that should now match, and a positive whose
-- conversation moved to a different listing after a relist.
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
