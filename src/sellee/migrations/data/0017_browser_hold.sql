-- A live claim on the one shared Chrome tab by a caller the daemon is not driving — a person
-- signing in. Every lane that would navigate yields to a row here, because the existing gates ask
-- only whether a *pass* is driving, and an interactive sign-in is not a pass.
--
-- `holder` names the claimant, so separate claims (an installer phase, a lone `sellee connect`)
-- release independently. `expires_ts` makes this a hold, not a lock: the claimant is a CLI blocked
-- on a human and can die without cleaning up, and a claim left behind should stop the agent until
-- the deadline, not forever. Like market_connect_requests, a handoff, not history.

CREATE TABLE browser_holds (
    holder     TEXT PRIMARY KEY,
    reason     TEXT NOT NULL,
    claimed_ts REAL NOT NULL,
    expires_ts REAL NOT NULL
);
