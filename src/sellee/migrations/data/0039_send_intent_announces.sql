-- What a past-enquirer update told its buyer: `price_drop`, `new_photos`, or both comma-joined.
-- NULL for every other send. Recorded at reserve, so the commit that sends it and a later settle
-- from the page both owe the seller the same tell, once.
ALTER TABLE send_intents ADD COLUMN announces TEXT;

-- When a past-enquirer update last committed on a thread. Kept apart from `last_followup_ts`, which
-- any follow-up stamps, so a nudge never stops a price drop from reaching its buyer.
ALTER TABLE threads ADD COLUMN last_update_ts REAL;
