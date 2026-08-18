-- Items, their confidential floors, and the pass queue. The floor lives in its own table so
-- no tool-facing read of an item can accidentally return it; only engine-side code and the
-- publish gate load floors. listing_urls is a JSON map {market: url}, written only after a
-- live verify. The passes table is the seam the pass lane claims from single-flight.
CREATE TABLE items (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    condition    TEXT,
    list_price   REAL,
    currency     TEXT,
    status       TEXT NOT NULL DEFAULT 'draft',
    listing_urls TEXT NOT NULL DEFAULT '{}',
    created_ts   REAL NOT NULL,
    updated_ts   REAL NOT NULL
);

CREATE TABLE floors (
    item_id    TEXT PRIMARY KEY REFERENCES items (id) ON DELETE CASCADE,
    floor      REAL NOT NULL,
    currency   TEXT,
    source     TEXT NOT NULL CHECK (source IN ('seller', 'default')),
    updated_ts REAL NOT NULL
);

CREATE TABLE passes (
    pass_id      TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    payload      TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'error')),
    rc           INTEGER,
    class        TEXT,
    summary      TEXT,
    requested_ts REAL NOT NULL,
    started_ts   REAL,
    finished_ts  REAL
);

CREATE INDEX idx_passes_status ON passes (status);
