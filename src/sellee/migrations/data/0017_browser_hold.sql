-- A person is signing in, and the lanes must not navigate the tab out from under them.
--
-- There is one shared Chrome tab. Two gates protected it and both asked the same question — is a
-- *pass* driving? — because a pass was the only thing that ever did. An interactive sign-in is not
-- a pass, so the window between "we opened the marketplace" and "the seller pressed Enter" was
-- invisible: the daemon believed the tab was free.
--
-- What that cost, in a real install on 2026-09-01: setup signed the seller in to Facebook, queued
-- the look at their existing listings that a fresh connection earns, then opened Carousell in the
-- same tab and blocked waiting for them to finish. Sixty seconds later the survey lane ticked, saw
-- no pass, and navigated to Facebook's listings page — over a half-typed Carousell login. Every
-- minute after that, again.
--
-- So the sign-in gets to say it holds the tab. A row here is a live claim on the browser by
-- something the daemon is not itself driving, and every lane that would navigate yields to it.
--
-- Two columns carry the whole design. `holder` names who claimed it, so the installer's claim
-- across a whole marketplace phase and a lone `sellee connect` are separate rows that release
-- independently rather than one clobbering the other. `expires_ts` is why this is a hold and not a
-- lock: the claimant is a CLI blocked on a human, and a CLI can be closed, killed, or Ctrl-C'd with
-- no chance to clean up. A lock left by a dead process stops the agent forever; a hold left by one
-- stops it until the deadline and no longer.
--
-- Deliberately not durable state anyone reads later: like market_connect_requests above, this is a
-- handoff, not a history. Nothing outside the yield checks ever looks at it.

CREATE TABLE browser_holds (
    holder     TEXT PRIMARY KEY,
    reason     TEXT NOT NULL,
    claimed_ts REAL NOT NULL,
    expires_ts REAL NOT NULL
);
