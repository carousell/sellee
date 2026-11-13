-- What a browser revision changes for a buyer, recorded when the seller asks, because by settle
-- time the item already holds the new values. A row without them tells nobody.
ALTER TABLE listing_revisions ADD COLUMN old_price REAL;
ALTER TABLE listing_revisions ADD COLUMN new_price REAL;
ALTER TABLE listing_revisions ADD COLUMN photo_added INTEGER NOT NULL DEFAULT 0;

-- Set once a `done` row owes its buyers nothing more, so a row tells its buyers once.
ALTER TABLE listing_revisions ADD COLUMN buyers_closed_ts REAL;

-- Edits that settled before this owe their buyers nothing.
UPDATE listing_revisions SET buyers_closed_ts = COALESCE(finished_ts, created_ts)
WHERE status = 'done';
