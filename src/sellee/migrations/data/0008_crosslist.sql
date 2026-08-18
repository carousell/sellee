-- Fan-out bookkeeping: whether a background publish has been reported to the seller yet.

-- A publish the seller triggered reports itself in the conversation that triggered it. One the
-- daemon triggered has no conversation, so the outcome is delivered by a sweep over settled rows
-- and this is the flag that keeps it delivered exactly once.
ALTER TABLE passes ADD COLUMN reported INTEGER NOT NULL DEFAULT 0;

-- Everything that already ran predates the fan-out, so nothing is retro-reported on upgrade.
UPDATE passes SET reported = 1;
