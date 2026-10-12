-- The message each buyer backed out with. A buyer who withdrew is answered, never chased: a send to
-- them has to answer this message or one they wrote after it. One row per buyer and item; a buyer
-- who comes back and backs out again moves it to their newer message.
CREATE TABLE buyer_withdrawals (
    item_id   TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    msg_id    TEXT NOT NULL,
    ts        REAL NOT NULL,
    PRIMARY KEY (item_id, thread_id)
);

-- A buyer already marked withdrawn has no row to answer from, and so could never be answered again.
-- Anchor them on the newest message they had written.
INSERT INTO buyer_withdrawals (item_id, thread_id, msg_id, ts)
SELECT nb.item_id, nb.thread_id, newest.msg_id, newest.ts
FROM negotiation_buyers nb
JOIN thread_messages newest ON newest.rowid = (
    SELECT m.rowid FROM thread_messages m
    WHERE m.thread_id = nb.thread_id AND m.dir = 'in'
    ORDER BY m.ts DESC, m.rowid DESC LIMIT 1
)
WHERE nb.status = 'withdrew';
