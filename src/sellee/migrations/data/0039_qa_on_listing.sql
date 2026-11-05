-- 1 when the seller answered a question suggested at listing time: the answer is then shown at the
-- end of the published description. Answers banked before this, and escalation answers, stay off.
ALTER TABLE qa_bank ADD COLUMN on_listing INTEGER NOT NULL DEFAULT 0 CHECK (on_listing IN (0, 1));
