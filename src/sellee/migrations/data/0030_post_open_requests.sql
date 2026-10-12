-- A sold item's post the seller asked to have opened in sellee's Chrome, to close it themselves.
-- One row per item, apart from sign-in requests, so no tap replaces another kind.
CREATE TABLE post_open_requests (
    item_id      TEXT PRIMARY KEY,
    market       TEXT NOT NULL,
    url          TEXT NOT NULL,
    requested_ts REAL NOT NULL
);
