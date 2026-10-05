-- A sold item's post the seller asked to have opened in sellee's Chrome, to close it themselves:
-- a Craigslist post's manage page, which only that Chrome is signed in to. One row per item, so
-- a second tap replaces the first and other items' requests and sign-ins are left alone.
CREATE TABLE post_open_requests (
    item_id      TEXT PRIMARY KEY,
    market       TEXT NOT NULL,
    url          TEXT NOT NULL,
    requested_ts REAL NOT NULL
);
