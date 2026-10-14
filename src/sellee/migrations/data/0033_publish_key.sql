-- The carousell.ai publish an item has under way: the idempotency key its create_listing calls
-- carry, and every request sent under it. Both are cleared once the publish settles.
ALTER TABLE items ADD COLUMN publish_key TEXT;
ALTER TABLE items ADD COLUMN publish_requests TEXT;
