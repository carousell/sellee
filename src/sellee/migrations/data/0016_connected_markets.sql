-- `crosslist_markets` becomes `connected_markets`, and grows a second job.
--
-- The setting used to answer one question — where else should a listing be posted — while reading a
-- marketplace inbox was ungated: the read lane worked every browser market it had an adapter for.
-- With one adapter that was invisible. With a second, every seller starts being told to sign in to a
-- marketplace they never asked about, so the list now gates the work as well as the publish, and its
-- name has to say so.
--
-- Three steps, and the last two are the ones that are easy to miss.
--
-- The ledger key must move with the setting. `settings.decide` looks the spec up by the stored key
-- and answers "that setting no longer exists" on a miss, so a rename touching only the settings row
-- would break approval on a proposal already awaiting a tap, and Undo on one already applied — Undo
-- reads the `applied` row. Every row moves, whatever its status.
--
-- The backfill is what keeps a working install working. Because reading was never gated, a seller
-- can be having their Carousell inbox read right now with this setting empty or explicitly cleared —
-- they never enabled cross-listing, and never needed to. Renaming alone would leave them connected
-- to nothing, the lane would stop reading, and buyers mid-conversation would simply stop being
-- answered. So a market they were demonstrably being read on is carried over, unioned into whatever
-- they had rather than replacing it: someone who really does want it off can turn it off again,
-- where a seller stranded mid-conversation has no way to even find out that is what happened.
--
-- The evidence is scoped deliberately narrowly: `source = 'browser_read'` (a thread the read lane
-- itself adopted, which is what shows the lane was working that market) and `market = 'carousell'`
-- (the only adapter that has ever shipped, so the only market that evidence can honestly point at).
-- A union over every distinct `threads.market` would be wrong — `create_thread` does not check the
-- market against the registry, so an arbitrary identifier in that column would become a connected
-- marketplace. Anything else a seller wants connected, they connect.
UPDATE settings SET key = 'connected_markets' WHERE key = 'crosslist_markets';

UPDATE pending_setting_changes SET key = 'connected_markets' WHERE key = 'crosslist_markets';

INSERT INTO settings (key, value, updated_ts)
SELECT 'connected_markets', '[]', COALESCE((SELECT MAX(updated_ts) FROM settings), 0)
WHERE NOT EXISTS (SELECT 1 FROM settings WHERE key = 'connected_markets')
  AND EXISTS (
      SELECT 1 FROM threads WHERE source = 'browser_read' AND market = 'carousell'
  );

UPDATE settings
SET value = (
    SELECT json_group_array(m) FROM (
        SELECT je.value AS m FROM json_each(settings.value) AS je
        UNION
        SELECT DISTINCT market FROM threads
        WHERE source = 'browser_read' AND market = 'carousell'
        ORDER BY m
    )
)
WHERE key = 'connected_markets'
  AND EXISTS (
      SELECT 1 FROM threads WHERE source = 'browser_read' AND market = 'carousell'
  );
