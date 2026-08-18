-- Event / transcript store, initial schema. The event row spine: a single autoincrement
-- ordering key, the journal clock stamped at write, an optional pass id, the kind, and a
-- JSON payload. Foreign/transport clocks only ever ride inside payload.
CREATE TABLE events (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    pass_id TEXT,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX idx_events_pass_id ON events (pass_id);
CREATE INDEX idx_events_ts ON events (ts);
