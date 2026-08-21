-- Give the bind nonce a deadline. An armed channel with no expiry stays adoptable forever, and the
-- connect flow deliberately copies the nonce off the machine (the CLI tells the seller to relay the
-- link to their phone), so a bind that is started and never finished leaves a live secret sitting in
-- a chat history with nothing to retire it.
--
-- Its own column rather than reusing updated_ts: advance_offset bumps updated_ts on every batch of
-- unattributable pre-bind traffic, so a stranger messaging the bot would extend the nonce's life
-- instead of letting it lapse.
--
-- Nullable, and readers treat NULL as already expired — a row armed before this migration has an
-- unbounded nonce by definition, and the less-capable state is the safe one to fail into.

ALTER TABLE channel ADD COLUMN bind_nonce_expires_ts REAL;
