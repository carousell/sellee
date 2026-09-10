-- A sign-in request is about a *target*, not only a marketplace.
--
-- Craigslist is the first market that needs two sign-ins: the site, and the mailbox its buyers reach
-- the seller through. A mailbox is not a marketplace and must not become one — `connected_markets`
-- is the seller's marketplace opt-in, and a Gmail "market" would attribute Craigslist conversations
-- to Gmail and offer a marketplace to list on with no listings.
--
-- So the thing a seller signs in to is generalised instead. The ids are `craigslist` and
-- `craigslist-mail`, resolved by `sellee/connectables.py`, and every market keeps its existing id —
-- so every stored row, callback token and CLI verb that already names one keeps working.
--
-- Renamed rather than left alone because a column called `market` holding `craigslist-mail` is a
-- lie the next reader has to discover. The rows here are a handoff between two threads and never a
-- history (written by the provider's receive loop, deleted by the connect lane once served), so
-- there is nothing here whose loss would matter even if a rename dropped it — RENAME preserves them
-- regardless.

ALTER TABLE market_connect_requests RENAME TO connect_requests;
ALTER TABLE connect_requests RENAME COLUMN market TO target;
