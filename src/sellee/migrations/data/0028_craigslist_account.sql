-- The seller's Craigslist account, which sellee signs up for and logs in to through the registration
-- address. No row means no account. `link` is the emailed link waiting to be opened.
CREATE TABLE craigslist_account (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    state            TEXT NOT NULL CHECK (
                         state IN (
                             'signup_requested', 'awaiting_activation', 'active',
                             'awaiting_login_link'
                         )
                     ),
    link             TEXT,
    -- When the current wait began: the sign-up request, or the activation mail being asked for.
    requested_ts     REAL NOT NULL,
    late_reported    INTEGER NOT NULL DEFAULT 0,
    updated_ts       REAL NOT NULL
);
