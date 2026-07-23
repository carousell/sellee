-- The channel subsystem's durable state: the bound-channel record, the durable inbox, the
-- needs-me notice queue, and the pause control row. Everything the seller must eventually see
-- lives here in selly.db, never only behind Telegram's cursor or in the deletable event store.

-- The bound channel — a singleton row (id forced to 1). Three durable poller states read off
-- this row: no token file → off; token + bind_nonce, chat_id NULL → awaiting-bind; chat_id set →
-- bound. bind_nonce authenticates the bind: only the chat presenting it in a /start payload ever
-- binds, so a stranger messaging mid-bind can never be adopted. update_offset is the Telegram
-- cursor, advanced only inside the same transaction that makes an inbox batch durable (never a
-- separate JSON file — the poisoned-offset silent-ACK footgun).
CREATE TABLE channel (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    adapter       TEXT NOT NULL DEFAULT 'telegram' CHECK (adapter IN ('telegram')),
    bot_username  TEXT,
    chat_id       INTEGER,
    update_offset INTEGER NOT NULL DEFAULT 0,
    bind_nonce    TEXT,
    welcomed_at   REAL,
    commands_hash TEXT,
    bound_ts      REAL,
    updated_ts    REAL NOT NULL
);

-- The durable inbox: every inbound update, persisted before it is acted on. event_id is the
-- Telegram update_id — UNIQUE, so a re-delivered batch (after a crash before the cursor advanced)
-- dedups by the constraint, not by a read-then-write race. received_ts is the local journal clock
-- stamped at write; src_ts carries Telegram's own message date as payload only, never for ordering.
CREATE TABLE channel_inbox (
    id          INTEGER PRIMARY KEY,
    event_id    INTEGER NOT NULL UNIQUE,
    kind        TEXT NOT NULL CHECK (kind IN ('command', 'text', 'action', 'photo')),
    text        TEXT,
    payload     TEXT,
    media_paths TEXT,
    src_ts      REAL,
    received_ts REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'claimed', 'handled', 'failed')),
    handled_by  TEXT,
    pass_id     TEXT,
    updated_ts  REAL NOT NULL
);

CREATE INDEX idx_channel_inbox_status ON channel_inbox (status);

-- The needs-me notice queue: outbound messages/notifications for the seller. Unbound, rows simply
-- accumulate and catchup is the delivery path; bound, the notice-drain lane sends them FIFO and
-- stamps them delivered. attempts counts delivery tries so a repeatedly-failing send stays visible
-- in catchup, never silently dropped.
CREATE TABLE notices (
    id           INTEGER PRIMARY KEY,
    text         TEXT NOT NULL,
    ref          TEXT,
    created_ts   REAL NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'delivered')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    delivered_ts REAL,
    via          TEXT CHECK (via IN ('channel', 'catchup')),
    pass_id      TEXT
);

CREATE INDEX idx_notices_status ON notices (status);

-- The pause control flag — a singleton row (id forced to 1). A paused daemon runs but acts on
-- nothing: the pass lane claims nothing, send-capable tools refuse, a running pass is killed. A
-- missing row reads as NOT paused (fail toward not-paused) so a corrupt state can never strand
-- the agent paused forever.
CREATE TABLE control (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    paused   INTEGER NOT NULL DEFAULT 0,
    since_ts REAL,
    source   TEXT
);
