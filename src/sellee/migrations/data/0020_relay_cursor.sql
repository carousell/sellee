-- Where the relay lane's list_threads poll resumes: bazaar's opaque cursor, stored only after the
-- threads it covered are written.
CREATE TABLE relay_cursor (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    cursor      TEXT NOT NULL,
    updated_ts  REAL NOT NULL
);

-- Relay threads to read again on every tick, whatever the cursor says: one whose listing no item
-- has yet, whose agent reply is still owed a send, whose block could not close it yet, or whose
-- read failed. bazaar does not list a thread again for any of these, so the lane must remember it.
CREATE TABLE relay_rereads (
    bazaar_thread_id  TEXT PRIMARY KEY,
    listing_id        TEXT NOT NULL,
    placed            INTEGER NOT NULL,
    added_ts          REAL NOT NULL
);
