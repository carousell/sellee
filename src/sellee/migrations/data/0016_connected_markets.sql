-- `crosslist_markets` becomes `connected_markets`, and gates reading as well as publishing.
--
-- The rename must reach every row: the settings row, and the pending/applied ledger rows keyed by
-- the stored name, or approval on a change still awaiting a tap and Undo on one already applied
-- break on a key they can no longer resolve.
--
-- The backfill keeps working installs working: reading was ungated before, so a seller can be
-- mid-conversation on Carousell with this setting empty or cleared. A market they were
-- demonstrably being read on is carried over, unioned into whatever they had rather than
-- replacing it. The evidence is scoped that narrowly because `create_thread` does not check
-- `threads.market` against the registry, so a wider union would promote arbitrary identifiers
-- into connected marketplaces.
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
