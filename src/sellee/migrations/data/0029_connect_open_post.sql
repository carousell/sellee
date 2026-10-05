-- A connect request may also open one page of a market for the seller: a Craigslist post's
-- manage page, which only sellee's Chrome is signed in to. `url` is that page; NULL otherwise.
CREATE TABLE market_connect_requests_new (
    market       TEXT PRIMARY KEY,
    mode         TEXT NOT NULL CHECK (mode IN ('open', 'probe', 'post')),
    url          TEXT,
    requested_ts REAL NOT NULL
);
INSERT INTO market_connect_requests_new (market, mode, url, requested_ts)
    SELECT market, mode, NULL, requested_ts FROM market_connect_requests;
DROP TABLE market_connect_requests;
ALTER TABLE market_connect_requests_new RENAME TO market_connect_requests;
