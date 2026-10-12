-- The last read of each Craigslist post, so a missing page retires a post only after it was once
-- seen live or reads missing twice in a row: a new post can take a while to be served.
CREATE TABLE post_reads (
    url       TEXT PRIMARY KEY,
    state     TEXT NOT NULL,
    seen_live INTEGER NOT NULL DEFAULT 0,
    read_ts   REAL NOT NULL
);
