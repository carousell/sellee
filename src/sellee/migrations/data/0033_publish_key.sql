-- The carousell.ai publish an item has under way: the idempotency key its create_listing calls
-- carry, every request sent under it, and when it began. All are cleared once the publish settles.
ALTER TABLE items ADD COLUMN publish_key TEXT;
ALTER TABLE items ADD COLUMN publish_requests TEXT;
ALTER TABLE items ADD COLUMN publish_started_ts REAL;
