-- Cross-link bookkeeping: the last set of external listing URLs the rail accepted per item.

-- The rail replaces an item's whole external-URL set on every update, so knowing whether a push
-- is owed means remembering what was last pushed — we are the field's only writer, which is what
-- makes a local marker sufficient. pushed_urls is the canonical JSON of the accepted set, so
-- "does the rail need an update" is a string compare and the row doubles as debuggable state.
-- Its own table rather than an items column: item rows are handed to the LLM by the tools, and
-- daemon-side sync bookkeeping is noise there (the floors table is the precedent).
CREATE TABLE crosslink_pushes (
    item_id     TEXT PRIMARY KEY REFERENCES items (id) ON DELETE CASCADE,
    pushed_urls TEXT NOT NULL,
    pushed_ts   REAL NOT NULL
);
