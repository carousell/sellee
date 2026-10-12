-- The buyer a withdrawal notice offered the seller instead, keyed by the notice that carries the
-- "Send them the link" tap. The notice text names them only by a display name, which need not be
-- unique, and the tap can come long after; this row is what the tap meant. `sent_ts` is set once
-- the link has gone to them, so acting on the tap again never sends it twice.
CREATE TABLE next_buyer_asks (
    notice_id  INTEGER PRIMARY KEY,
    item_id    TEXT NOT NULL,
    thread_id  TEXT NOT NULL,
    offer      REAL NOT NULL,
    created_ts REAL NOT NULL,
    sent_ts    REAL
);
