-- When an item last went live: stamped as its first listing URL is recorded, and cleared once the
-- last one is taken away. A listing live a week that no buyer has written about is told to the
-- seller as quiet, and created_ts would date a draft listed long after it was written.
ALTER TABLE items ADD COLUMN live_since_ts REAL;

-- Whatever is live now has been live at least since it was created; nothing older is known.
UPDATE items SET live_since_ts = created_ts
WHERE EXISTS (SELECT 1 FROM json_each(items.listing_urls) WHERE json_each.value != '');

-- A sale the seller chose to close themselves, recorded when the escalation that asked how to
-- close is resolved that way. Until it is confirmed sold, released, or the buyer backs out
-- (settled_ts), the seller is asked once a day whether it went through.
CREATE TABLE seller_handled_deals (
    thread_id     TEXT PRIMARY KEY,
    item_id       TEXT NOT NULL,
    escalation_id TEXT NOT NULL,
    handed_ts     REAL NOT NULL,
    settled_ts    REAL
);
CREATE INDEX seller_handled_deals_item ON seller_handled_deals (item_id);

-- The update lane looks, every minute, for whether a slow tell was already written for an
-- escalation, a listing or a sale.
CREATE INDEX tells_kind ON tells (kind, created_ts);
