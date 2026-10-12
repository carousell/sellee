-- The seller's Craigslist account, which they create in sellee's Chrome with their registration
-- address. No row means no account. `link` is the latest emailed link; it stays once opened, so
-- a re-read mail cannot reopen it.
CREATE TABLE craigslist_account (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    state            TEXT NOT NULL CHECK (
                         state IN ('awaiting_activation', 'active', 'awaiting_login_link')
                     ),
    link             TEXT,
    link_opened      INTEGER NOT NULL DEFAULT 0,
    -- When the current wait began.
    requested_ts     REAL NOT NULL,
    late_reported    INTEGER NOT NULL DEFAULT 0,
    updated_ts       REAL NOT NULL,
    -- The registration address the account reached sellee through.
    address          TEXT,
    -- When the seller last asked to sign in themselves: the next login link Craigslist sends is
    -- theirs.
    seller_login_ts  REAL,
    -- The seller's own email, when they signed in with their own account rather than the
    -- registration address: sellee posts from it, but none of its mail reaches sellee.
    own_email        TEXT
);
