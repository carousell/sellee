-- The browser layer's durable state: the Q&A bank the reply loop answers from, the selector
-- cache the browser flows heal into, and the scam verdict stamped on scripted-read inbound.

-- Answers the seller has taught the agent, so the same buyer question is never escalated twice.
-- item_id = '*' is a global entry (applies to every item). source is CHECK-constrained to
-- 'seller': the only answers banked today are ones the seller gave.
CREATE TABLE qa_bank (
    id         INTEGER PRIMARY KEY,
    item_id    TEXT NOT NULL,
    question   TEXT NOT NULL,
    answer     TEXT NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('seller')),
    created_ts REAL NOT NULL
);

CREATE INDEX idx_qa_bank_item ON qa_bank (item_id);

-- The per-market selector cache ("page memory"): where each browser control was last found, so a
-- routine pass skips the snapshot+vision round-trip per field. A hint layer only. It stores
-- DOM-locating strings and timestamps ONLY: never a value, price, or address. page_url_pattern is
-- the page guard; a row is never trusted on the wrong page, so record refuses its absence in code.
CREATE TABLE ui_cache (
    market           TEXT NOT NULL,
    flow             TEXT NOT NULL,
    step             TEXT NOT NULL,
    strategy         TEXT NOT NULL CHECK (strategy IN ('css', 'aria', 'role', 'text')),
    query            TEXT NOT NULL,
    action_kind      TEXT NOT NULL DEFAULT '',
    page_url_pattern TEXT NOT NULL,
    recorded_at      REAL NOT NULL,
    last_verified_at REAL,
    last_ok_at       REAL,
    fail_count       INTEGER NOT NULL DEFAULT 0,
    -- advisory observability only (how warm the cache is); never read by the staleness predicate
    ok_streak        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (market, flow, step)
);

-- The daemon-side scam pre-scan stamps its verdict on every scripted-read inbound row
-- (clean | suspicious | scam); NULL on rows that predate the scan or arrived by another path.
ALTER TABLE thread_messages ADD COLUMN scam_verdict TEXT;
