-- A marketplace that has told us to stop, and will not be driven again until it says otherwise.
--
-- On 2026-09-09 Facebook served the seller's account a full-page warning: "We suspect automated
-- behavior on your account ... ensure that no other users or tools have access to your account".
-- Nothing in the agent could see it. The read lane classified the failed read as "the marketplace
-- declined", told the seller once that Facebook would not hand over their conversations, and went
-- on requesting `/messages/` every three hundred seconds from the same account, indefinitely. The
-- reply, survey and publish paths never consulted any of it and carried on too. From the other
-- side that is an account that was warned, acknowledged nothing, and changed nothing — which is
-- the trajectory that precedes a restriction.
--
-- So this row is the brake, and it is durable for the reason the in-process counters are not. A
-- wall makes a restart likely: the seller is being told something is wrong and the natural
-- response is to restart the agent or reboot the laptop. Every other brake in the read lane lives
-- in `InboxDeps` and is documented as erring toward reading MORE after a restart, which is right
-- for a fault of ours and exactly inverted for this. A restart must not be what un-brakes an
-- account that has been warned.
--
-- `expires_ts` is nullable and the distinction is the point:
--   * a value means the block decays on its own — for a wall that may simply pass;
--   * NULL means indefinite, and only an administrative clear lifts it. That is for `restricted`,
--     where there is nothing a button can do and pretending otherwise would resume traffic against
--     an account Meta has already acted on.
--
-- `incident_ts` is what makes the seller notice truthful across time. `told_ts` alone would say a
-- block was announced and stay set, so a block that expired and re-triggered — a new thing to say
-- — would be silenced by the record of the last one. The notice is re-armed when the incident
-- changes, never merely when a strike increments.
--
-- `strikes` counts how many times this market has hit a wall without a clean probe in between, so
-- the window can escalate rather than repeating.
--
-- One row per market, replaced rather than stacked: a market is blocked or it is not, and a
-- history of that belongs in the event log, which already has it.

CREATE TABLE market_blocks (
    market      TEXT PRIMARY KEY,
    cause       TEXT NOT NULL,
    strikes     INTEGER NOT NULL DEFAULT 1,
    blocked_ts  REAL NOT NULL,
    incident_ts REAL NOT NULL,
    expires_ts  REAL,
    told_ts     REAL
);
