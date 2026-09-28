-- Changing a listing that is already up: one row per (item, marketplace) edit still owed.
--
-- The rail is edited inline by the tool that takes the seller's instruction — it is one API call
-- and it either worked or it did not. A browser marketplace is minutes of driving someone's
-- logged-in account, so it cannot be done in the turn that asks for it. This table is the queue
-- that makes the difference survivable: the work is derived from durable rows, so a restart
-- resumes it, attempts are bounded because each one is expensive, and the seller is told how each
-- marketplace went by the daemon reading these rows back — never by a model remembering to.
--
-- `changed` holds field NAMES, not values. The lane re-reads the item when it runs, so a listing
-- converges on the item record rather than on a snapshot that a second edit has already made
-- wrong; the names are what the driver touches and what the report names. Queueing an edit for a
-- pair that already has a pending row supersedes that row instead of stacking a second one — a
-- seller changing their mind twice must not produce two drives.
--
-- `accepted` is what that marketplace was actually shown to be holding afterwards, read back off
-- its own page. It is the only place the divergence is recorded: an edit that lands on the rail
-- and fails on Facebook leaves two public prices for one item, and nothing else in the tree
-- compares a stored price to a live one (reconcile matches titles and listing ids, never money).
CREATE TABLE listing_revisions (
    revision_id TEXT PRIMARY KEY,
    item_id     TEXT NOT NULL,
    market      TEXT NOT NULL,
    changed     TEXT NOT NULL,
    accepted    TEXT,
    status      TEXT NOT NULL CHECK (
                    status IN ('pending', 'running', 'done', 'failed', 'superseded')
                ),
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    -- When the latest attempt started. Spaces retries (an attempt is minutes of browser work, and
    -- three in two minutes is one attempt three times), and dates a row a crash left 'running'.
    claimed_ts  REAL,
    -- The edit pass driving this row, for a market with a recipe rather than a driver.
    pass_id     TEXT,
    reported    INTEGER NOT NULL DEFAULT 0,
    created_ts  REAL NOT NULL,
    finished_ts REAL
);

-- The lane selects by status on every tick (pending rows to claim, settled rows to report) and
-- never by item or market alone.
CREATE INDEX listing_revisions_status ON listing_revisions (status);
