-- The seller-facing settings surface: the runtime-settings store, the propose→approve→apply
-- change ledger, and the urgency flag that lets escalation notices bypass quiet hours.
--
-- These are distinct from seller_config (domain records: basics/shipping/origin) and from the
-- operator config file (install knobs with restart semantics). A setting is a behavior knob the
-- seller changes at runtime through a door; its schema (type, validation, rendering, default,
-- approval policy) lives in code, not here — so an unset key is not a row, it reads as its
-- registry default, and a new setting needs no backfill.

-- One row per set setting, keyed by the registry key. value is the canonical JSON encoding of the
-- registry-parsed value; prior_value/prior_ts snapshot the value this row replaced, so single-level
-- undo restores the immediately-preceding state without a version-history table. An unset key has
-- no row at all (it reads as the registry default).
CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_ts  REAL NOT NULL,
    prior_value TEXT,
    prior_ts    REAL
);

-- The change ledger: the LLM proposes, deterministic daemon code decides and applies. A proposal
-- is written pending; the daemon then either applies it immediately (low-stakes) or holds it for a
-- human signal through a door (approval required). value/prior_value are canonical JSON snapshots
-- taken at proposal time, so applying a held change never re-reads a value that may have moved.
-- decided_via records which door settled it (button/token on the channel, cli attended, auto for
-- an immediate apply). One live (pending) proposal per key is a policy the store enforces, not a
-- constraint here — a new proposal supersedes the old.
CREATE TABLE pending_setting_changes (
    change_id   TEXT PRIMARY KEY,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    prior_value TEXT,
    status      TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'applied', 'cancelled', 'expired', 'superseded')),
    proposed_ts REAL NOT NULL,
    decided_ts  REAL,
    decided_via TEXT CHECK (decided_via IN ('button', 'token', 'cli', 'auto'))
);

CREATE INDEX idx_pending_setting_changes_key_status ON pending_setting_changes (key, status);

-- Notice hold policy: holdable = 1 marks a proactive/background notice the drain lane may defer to
-- the end of quiet hours. Everything seller-facing — a channel-pass reply, a settings approval, an
-- escalation push — is holdable = 0 and delivered at any hour, so seller-initiated chat is never
-- gated (a seller messaging at 3am has opted in). The flag is on notices, not a separate table, so
-- the drain reads it in the same row it already claims. Nothing sets holdable = 1 yet — proactive
-- marketplace pushes arrive with the poll lanes; the machinery ships now.
ALTER TABLE notices ADD COLUMN holdable INTEGER NOT NULL DEFAULT 0;

-- Optional provider-neutral controls for a notice: a JSON list of [label, token] button pairs an
-- approval or echo notice carries (Approve/Cancel, Undo), rendered into the channel's native
-- keyboard at delivery. NULL for an ordinary text notice. Kept on the notice so a keyboard survives
-- a crash the same way its text does — delivery is a pure read of the durable row.
ALTER TABLE notices ADD COLUMN controls TEXT;
