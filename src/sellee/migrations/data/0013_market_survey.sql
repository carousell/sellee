-- Taking over the listings a seller already had: what a survey found, what the seller said about
-- it, and what is still owed.
--
-- Signing in to a marketplace used to give the agent an inbox and an account and nothing else —
-- everything the seller already had listed stayed invisible, because a buyer conversation is only
-- adopted when it names a listing we hold an item for. The survey reads the seller's own listings
-- once per market; an adopted row becomes an item, and every existing lane applies from there.

-- One row per market, written by whichever trigger first sees that market signed in (the connect
-- lane after a sign-in, the read lane for one already connected). The market is the primary key
-- and the triggers insert with ON CONFLICT DO NOTHING, so this doubles as the one-ask-ever guard:
-- a 'done' row is only reset by a seller deliberately asking for a fresh look.
--
-- 'abandoned' is not 'done': the page could not be read (signed out again, the page moved), and
-- claiming `found = 0` would read as "this seller has nothing listed". Only an unserved outcome
-- counts against `attempts`, so a tick where a publish pass held the browser costs nothing.
CREATE TABLE market_surveys (
    market       TEXT PRIMARY KEY,
    state        TEXT NOT NULL CHECK (state IN ('due', 'done', 'abandoned')),
    requested_ts REAL NOT NULL,
    surveyed_ts  REAL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    found        INTEGER NOT NULL DEFAULT 0
);

-- The listings a survey found, and the seller's answer about them. Keyed by the marketplace's own
-- listing id, so a re-read can never reset a row that has been decided or adopted.
--
-- `price` is NOT NULL on purpose: the carousell.ai publish refuses an item with no price, so an
-- unpriced listing must never become a row the seller is asked about. Discovery drops those and
-- counts them into its event instead. `price_text` is the price exactly as rendered ("S$40"),
-- which is what the ask shows; the authoritative currency comes from the listing's own page.
--
-- `attempts` / `last_error` / status 'failed' are per listing: without them one unreadable listing
-- page would be retried at the head of the queue forever.
--
-- `rail_state` is the durable record that a carousell.ai publish is still owed, and it exists
-- because nothing else could recover one: the fan-out needs the rail listing as its precondition,
-- and queue_marketplace_publish refuses any market the seller has not enabled. A publish skipped
-- because another one held the slot stays 'owed' for a later tick; a failed one is retried a
-- bounded number of times before it is reported as failed.
CREATE TABLE discovered_listings (
    market        TEXT NOT NULL,
    listing_id    TEXT NOT NULL,
    url           TEXT NOT NULL,
    title         TEXT NOT NULL,
    price         REAL NOT NULL,
    price_text    TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL CHECK (
                      status IN ('pending', 'accepted', 'declined', 'expired', 'adopted', 'failed')
                  ),
    manage        TEXT CHECK (manage IN ('inbox', 'relist')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    item_id       TEXT,
    rail_state    TEXT CHECK (rail_state IN ('owed', 'queued', 'done', 'failed')),
    rail_pass_id  TEXT,
    rail_attempts INTEGER NOT NULL DEFAULT 0,
    discovered_ts REAL NOT NULL,
    decided_ts    REAL,
    adopted_ts    REAL,
    PRIMARY KEY (market, listing_id)
);

-- Both lane phases select by status (pending rows to expire, accepted rows to adopt, owed rows to
-- publish) and never by market alone.
CREATE INDEX discovered_listings_status ON discovered_listings (status);
