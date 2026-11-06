-- A buyer turned down under the old floor whose offer meets a lower one. `reopened` is set when
-- the seller lowers the floor and spent by that buyer's next decided offer, which is then weighed
-- against the new floor instead of being answered as a repeat of the one already decided.
ALTER TABLE negotiation_buyers ADD COLUMN reopened INTEGER NOT NULL DEFAULT 0;

-- The ledger status a reopened buyer stood at before the reopening made them active again, so a
-- reopening taken back (a floor raised back over their offer, or the item no longer taking offers
-- before they were decided again) returns them to it. NULL whenever `reopened` is 0.
ALTER TABLE negotiation_buyers ADD COLUMN reopened_from TEXT;

-- When a thread became owed a reply, whether or not the buyer had written since their last answer,
-- because a lower floor now reaches their offer. The reply lane claims it as if the buyer had
-- written; a reply whose sender was shown the owing clears it, and so does the buyer backing out
-- or losing the item, or the item selling.
ALTER TABLE threads ADD COLUMN reply_owed_ts REAL;

-- The owing (a `threads.reply_owed_ts`) the sender of this reply had in front of it when the reply
-- was reserved, or NULL when the thread was owed nothing then. Only a reply composed with sight of
-- the owing settles it, however long after the reservation it is confirmed sent.
ALTER TABLE send_intents ADD COLUMN owed_seen_ts REAL;
