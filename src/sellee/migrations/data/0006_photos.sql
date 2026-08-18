-- Listing photos on the item record.
--
-- photos is a JSON list of {path, uploaded_url?} in display order — the first entry is the
-- listing's cover. Every path lives inside the media store; the containment check runs in the
-- single writer rather than here, so a `..` traversal or a symlink pointing out of the store is
-- refused before a row is ever written. uploaded_url appears once the photo has been pushed to
-- the marketplace's media host, and the whole set is stamped at once — a half-uploaded set would
-- publish with the wrong cover.
ALTER TABLE items ADD COLUMN photos TEXT NOT NULL DEFAULT '[]';
