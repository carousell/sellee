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
