-- The notifications a marketplace has rung with, and whether the visit each one asked for happened.
--
-- On a marketplace that polices automation the agent opens the inbox only when the marketplace rings
-- — a notification the agent's Chrome displayed — and a person-sized moment afterwards, the way
-- someone glances at a ping and then opens the chat. Chrome's DevTools log of displayed
-- notifications replays everything it has recorded on every look, so this table is what makes a
-- ring heard once: keyed on the notification's own identity (its tag and the moment it was shown),
-- a replay adds nothing, and a restart neither loses a ring nor answers it twice.
--
-- `kind` is what the ring was. A 'message' is owed a visit once `due_ts` passes; anything else is
-- recorded and asks for nothing, kept as evidence the doorbell still works. `handled_ts` is set by
-- the read that answered it. Only identity and times are kept: what the notification said is read
-- properly, off the conversation itself, by the visit.
--
-- 24 and 25 are taken by the hosted work in flight on its own branch; see the note in
-- tests/test_migrations.py on why numbers are reserved rather than reused.

CREATE TABLE market_rings (
    market     TEXT NOT NULL,
    ring_key   TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('message', 'other')),
    shown_ts   REAL NOT NULL,
    heard_ts   REAL NOT NULL,
    due_ts     REAL NOT NULL,
    handled_ts REAL,
    PRIMARY KEY (market, ring_key)
);

CREATE INDEX market_rings_owed ON market_rings (market, handled_ts, due_ts);
