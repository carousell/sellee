-- Where the registration lane's list_registration_mail poll resumes: bazaar's opaque cursor, stored
-- after each page it covered is handled.
CREATE TABLE registration_cursor (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    cursor      TEXT NOT NULL,
    updated_ts  REAL NOT NULL
);

-- Registration mail already handled, so a page read twice acts on nothing twice. A mail that became
-- a thread message keeps its thread and subject, which the reply's subject is taken from.
CREATE TABLE registration_seen (
    mail_id      TEXT PRIMARY KEY,
    thread_id    TEXT,
    subject      TEXT NOT NULL,
    received_ts  REAL NOT NULL,
    seen_ts      REAL NOT NULL
);

CREATE INDEX registration_seen_thread ON registration_seen (thread_id, received_ts);
