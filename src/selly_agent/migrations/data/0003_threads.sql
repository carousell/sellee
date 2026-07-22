-- The thread/want/budget data model plus the money-engine ledgers, escalations, checkouts,
-- pacing, scam signatures, and seller config. One threads table carries both sides (a `side`
-- column), with identity NOT NULL at creation so an identity-less skeleton thread — the class
-- of bug that silently disabled own-echo/terminal suppression — cannot exist. Side-specific
-- columns are nullable; a CHECK enforces item_id on sell threads and want_id on buy threads.
-- Transcript rows normalize into thread_messages, deduped by a UNIQUE constraint, not by code.

ALTER TABLE items ADD COLUMN size_bucket TEXT;

-- The negotiation counter ladder reads step/rounds from the floor record.
ALTER TABLE floors ADD COLUMN auto_counter_step INTEGER;
ALTER TABLE floors ADD COLUMN auto_counter_rounds INTEGER;

CREATE TABLE wants (
    want_id        TEXT PRIMARY KEY,
    query          TEXT NOT NULL,
    category       TEXT,
    condition_pref TEXT,
    region         TEXT,
    currency       TEXT,
    target_price   REAL,
    status         TEXT NOT NULL DEFAULT 'searching' CHECK (status IN (
                       'searching', 'shortlisted', 'liaising', 'agreed',
                       'bought', 'abandoned', 'cancelled')),
    source         TEXT,
    candidates     TEXT NOT NULL DEFAULT '[]',
    shortlist      TEXT NOT NULL DEFAULT '[]',
    cancelled_ts   REAL,
    cancel_reason  TEXT,
    created_ts     REAL NOT NULL,
    updated_ts     REAL NOT NULL
);

CREATE TABLE threads (
    thread_id          TEXT PRIMARY KEY,
    side               TEXT NOT NULL CHECK (side IN ('sell', 'buy')),
    market             TEXT NOT NULL,
    item_id            TEXT REFERENCES items (id),
    want_id            TEXT REFERENCES wants (want_id),
    counterpart_handle TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'active',
    held_reason        TEXT,
    held_from_status   TEXT,
    buyer_location     TEXT,
    agent_note         TEXT,
    listing_url        TEXT,
    listed_price       REAL,
    close_method       TEXT,
    closed_ts          REAL,
    closed_reason      TEXT,
    cursor_last_msg_id TEXT,
    cursor_last_ts     REAL,
    last_followup_ts   REAL,
    followup_disposition TEXT,
    source             TEXT,
    created_ts         REAL NOT NULL,
    updated_ts         REAL NOT NULL,
    -- identity completeness: the side-required owning entity is present from creation
    CHECK (side <> 'sell' OR item_id IS NOT NULL),
    CHECK (side <> 'buy' OR want_id IS NOT NULL),
    -- per-side status sets (there is no 'sold_elsewhere' — sale-loss is 'lost'/'closed')
    CHECK (
        (side = 'sell' AND status IN (
            'active', 'liaising', 'agreed', 'seller_handling',
            'handover', 'lost', 'closed', 'escalated', 'held'))
        OR
        (side = 'buy' AND status IN (
            'active', 'liaising', 'agreed', 'held', 'escalated', 'closed'))
    )
);

CREATE INDEX idx_threads_side_status ON threads (side, status);
CREATE INDEX idx_threads_item ON threads (item_id);
CREATE INDEX idx_threads_want ON threads (want_id);

CREATE TABLE thread_messages (
    thread_id TEXT NOT NULL REFERENCES threads (thread_id) ON DELETE CASCADE,
    msg_id    TEXT NOT NULL,
    dir       TEXT NOT NULL CHECK (dir IN ('in', 'out')),
    text      TEXT NOT NULL,
    ts        REAL NOT NULL,
    source    TEXT,
    UNIQUE (thread_id, msg_id)
);

CREATE INDEX idx_thread_messages_thread ON thread_messages (thread_id, ts);

-- The send bracket: a durable intent recorded before the sink send, committed after. A crash
-- between the two is folded by a sweep as unconfirmed + escalate, never re-sent.
CREATE TABLE send_intents (
    intent_id    TEXT PRIMARY KEY,
    thread_id    TEXT NOT NULL REFERENCES threads (thread_id) ON DELETE CASCADE,
    in_msg_id    TEXT,
    text         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN (
                     'pending', 'sent_unverified', 'committed', 'unconfirmed')),
    created_ts   REAL NOT NULL,
    sent_ts      REAL,
    committed_ts REAL
);

CREATE INDEX idx_send_intents_status ON send_intents (status);

-- The buyer's confidential budget: never returned by any tool-reachable read, only the buyer
-- engine loads it. want_id is the PK; provenance mirrors the floor's seller/default.
CREATE TABLE budgets (
    want_id             TEXT PRIMARY KEY REFERENCES wants (want_id) ON DELETE CASCADE,
    max_budget          REAL NOT NULL,
    target_price        REAL,
    currency            TEXT,
    opening_ratio       REAL,
    auto_counter_step   INTEGER,
    auto_counter_rounds INTEGER,
    give_up_polls       INTEGER,
    source              TEXT NOT NULL CHECK (source IN ('buyer', 'default')),
    updated_ts          REAL NOT NULL
);

-- Sell-side negotiation ledger: one row per item, one negotiation_buyers row per pursuing thread.
CREATE TABLE negotiations (
    item_id      TEXT PRIMARY KEY REFERENCES items (id) ON DELETE CASCADE,
    state        TEXT NOT NULL CHECK (state IN (
                     'open', 'bidding', 'reserved_provisional', 'sold')),
    -- a sticky flag: once a leading bid arrives the item is a bidding item, and that survives a
    -- confirm-bid / release so the below-list path stays blocked and release returns to bidding.
    is_bidding   INTEGER NOT NULL DEFAULT 0,
    front_runner TEXT,
    sold_to      TEXT,
    updated_ts   REAL NOT NULL
);

CREATE TABLE negotiation_buyers (
    item_id       TEXT NOT NULL REFERENCES negotiations (item_id) ON DELETE CASCADE,
    thread_id     TEXT NOT NULL,
    buyer_handle  TEXT,
    offers        TEXT NOT NULL DEFAULT '[]',
    highest_offer REAL NOT NULL DEFAULT 0,
    rounds_used   INTEGER NOT NULL DEFAULT 0,
    last_counter  REAL,
    lowball_count INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'active',
    PRIMARY KEY (item_id, thread_id)
);

-- Buy-side negotiation ledger: one row per want, one seller row per pursued listing/thread.
CREATE TABLE buyer_negotiations (
    want_id          TEXT PRIMARY KEY REFERENCES wants (want_id) ON DELETE CASCADE,
    state            TEXT NOT NULL CHECK (state IN ('shopping', 'committed')),
    committed_thread TEXT,
    updated_ts       REAL NOT NULL
);

CREATE TABLE buyer_negotiation_sellers (
    want_id            TEXT NOT NULL REFERENCES buyer_negotiations (want_id) ON DELETE CASCADE,
    thread_id          TEXT NOT NULL,
    seller_handle      TEXT,
    listed_price       REAL,
    our_offers         TEXT NOT NULL DEFAULT '[]',
    our_highest_offer  REAL NOT NULL DEFAULT 0,
    seller_lowest_ask  REAL,
    rounds_used        INTEGER NOT NULL DEFAULT 0,
    last_offer         REAL,
    agreed_price       REAL,
    status             TEXT NOT NULL DEFAULT 'negotiating',
    PRIMARY KEY (want_id, thread_id)
);

-- Pacing ledger: the cap is keyed per marketplace account (sell + buy share one ledger); kind
-- is recorded for observability only, never a separate cap bucket.
CREATE TABLE pacing_actions (
    marketplace TEXT NOT NULL,
    kind        TEXT NOT NULL,
    ts          REAL NOT NULL
);

CREATE INDEX idx_pacing_actions_market_ts ON pacing_actions (marketplace, ts);

-- The local scam signature bank (the shipped registry is a package data file). The state
-- machine: observed -> confirmed -> shared; dismissed is terminal; seller_confirm is born confirmed.
CREATE TABLE scam_signatures (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    value        TEXT NOT NULL,
    play         TEXT,
    marketplace  TEXT,
    thread_id    TEXT,
    context      TEXT,
    detected_by  TEXT,
    severity     TEXT,
    status       TEXT NOT NULL CHECK (status IN (
                     'observed', 'confirmed', 'shared', 'dismissed')),
    added_ts     REAL NOT NULL,
    confirmed_ts REAL,
    shared_ts    REAL
);

-- Escalations: a real, scope-visible thread is required (no synthetic ids). One open escalation
-- per thread; resolve stamps the outcome and is the substrate any future alarm path checks first.
CREATE TABLE escalations (
    id              TEXT PRIMARY KEY,
    thread_id       TEXT NOT NULL REFERENCES threads (thread_id) ON DELETE CASCADE,
    side            TEXT,
    item_id         TEXT,
    want_id         TEXT,
    kind            TEXT,
    open_question   TEXT NOT NULL,
    context_summary TEXT,
    status          TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
    resolution      TEXT,
    created_ts      REAL NOT NULL,
    resolved_ts     REAL
);

CREATE INDEX idx_escalations_thread_status ON escalations (thread_id, status);

CREATE TABLE checkouts (
    sale_id      TEXT PRIMARY KEY,
    item_id      TEXT NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    thread_id    TEXT,
    checkout_url TEXT NOT NULL,
    price        REAL,
    currency     TEXT,
    issued_ts    REAL NOT NULL
);

-- Seller config, one JSON blob per section (basics / shipping / origin). The exact origin
-- address is stored here and never returned by a read tool — only the shipping engine reads it.
CREATE TABLE seller_config (
    section    TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_ts REAL NOT NULL
);
