-- The seller's Craigslist account, which sellee signs up for itself. No row means no account.
-- `link` is the latest emailed link; it stays once opened, so a re-read mail cannot reopen it.
CREATE TABLE craigslist_account (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    state            TEXT NOT NULL CHECK (
                         state IN (
                             'signup_requested', 'awaiting_activation', 'active',
                             'awaiting_login_link'
                         )
                     ),
    link             TEXT,
    link_opened      INTEGER NOT NULL DEFAULT 0,
    -- When the current wait began.
    requested_ts     REAL NOT NULL,
    late_reported    INTEGER NOT NULL DEFAULT 0,
    updated_ts       REAL NOT NULL
);
