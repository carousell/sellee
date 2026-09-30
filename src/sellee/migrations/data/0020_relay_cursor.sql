-- Where the relay lane's list_threads poll resumes: bazaar's opaque cursor, stored only after the
-- threads it covered are written. Number 20 is the slot 0022 reserved for the mail-relay branch.

CREATE TABLE relay_cursor (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    cursor      TEXT NOT NULL,
    updated_ts  REAL NOT NULL
);
