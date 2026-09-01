-- Buyers waiting in conversations we cannot place, and whether the seller has been told.
--
-- The notice that reports them was keyed on the SET of conversation ids the sweep happened to see,
-- held in memory. Both halves of that are wrong on a real install.
--
-- The set is not a fact about the seller's inbox; it is a fact about one read of it. A marketplace
-- list is a window onto the folder — Messenger unmounts rows far outside the viewport — so
-- consecutive sweeps see overlapping but different subsets, every difference reads as news, and the
-- seller gets the same twenty people announced again and again. In memory, a daemon restart
-- re-announces all of them from scratch.
--
-- `reported_ts` moves the question from "is this set different" to "is there anyone here I have not
-- mentioned", which is the question the seller actually wants answered, and it survives both a
-- jittery read and a restart.
--
-- The seller's answer needs somewhere to land too. "Don't manage those chats" was a decision they
-- made and nothing recorded, so the notice kept coming; the mute that records it is a `meta` key
-- (`unplaceable-muted:<market>`), because it is one flag per market and not a per-conversation
-- fact. Both are cleared together by `reopen_market_survey` — the seller asking to look at their
-- listings again is the documented way back, and it is what the notice's own text promises.
--
-- Growth is the same bargain `thread_listing_lookups` makes: one row per conversation ever seen,
-- never pruned, tens of bytes each, bounded by how many people message a seller.

CREATE TABLE unplaceable_conversations (
    thread_id     TEXT PRIMARY KEY,
    market        TEXT NOT NULL,
    first_seen_ts REAL NOT NULL,
    reported_ts   REAL
);

CREATE INDEX unplaceable_conversations_market ON unplaceable_conversations (market);
