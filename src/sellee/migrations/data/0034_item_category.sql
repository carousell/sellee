-- The item's category in carousell.ai's item policy, as one of the tokens that policy serves.
-- Items drafted before this have none.
ALTER TABLE items ADD COLUMN category TEXT;
