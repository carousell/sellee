-- Every offer the negotiation engine decided, written in the transaction that decided it, so a
-- lower minimum can later go back to the buyers it now reaches. below_floor is computed against
-- the floor the engine was handed and is the only trace of it kept here: the floor value itself
-- lives in `floors` alone, and no column here could hold it.
CREATE TABLE offer_decisions (
    id            INTEGER PRIMARY KEY,
    item_id       TEXT NOT NULL,
    thread_id     TEXT NOT NULL,
    amount        REAL NOT NULL,
    currency      TEXT NOT NULL,
    decision      TEXT NOT NULL,
    counter_price REAL,
    below_floor   INTEGER NOT NULL DEFAULT 0,
    ts            REAL NOT NULL
);
CREATE INDEX offer_decisions_item ON offer_decisions (item_id, ts);

-- A thing done on a seller's listing that they have not been told about, one row per fact,
-- written in the transaction that produced it. `kind` names the line it becomes and `facts` is
-- the JSON that line needs, rendered by code only. `decision` is 1 when the line asks the seller
-- for something. told_ts and notice_id are stamped once the notice carrying it is queued; a row
-- retired without being sent keeps notice_id NULL.
CREATE TABLE tells (
    id         INTEGER PRIMARY KEY,
    item_id    TEXT,
    thread_id  TEXT,
    kind       TEXT NOT NULL,
    decision   INTEGER NOT NULL DEFAULT 0,
    facts      TEXT NOT NULL,
    created_ts REAL NOT NULL,
    told_ts    REAL,
    notice_id  INTEGER
);
CREATE INDEX tells_untold ON tells (created_ts) WHERE told_ts IS NULL;

-- Whether the reply lane composed this send on its own. Only such a reply is news to the seller:
-- one they dictated through their channel, or sent at their own terminal, they already know about.
-- Recorded when the intent is reserved, so a commit and a later settle from the page agree.
ALTER TABLE send_intents ADD COLUMN autonomous INTEGER NOT NULL DEFAULT 0;

-- "Was another buyer on this item already answered" runs inside every reply commit; without this
-- it walks every committed intent ever sent.
CREATE INDEX send_intents_thread ON send_intents (thread_id, kind, status);
