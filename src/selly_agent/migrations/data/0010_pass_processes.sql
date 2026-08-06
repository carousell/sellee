-- The OS process behind a running pass, so a daemon can clean up what a previous one left behind.

-- A pass that outlives the daemon that started it keeps acting on the seller's live account, and
-- the only way a fresh daemon can recognise one is to have been told: this row is that record. The
-- creation time is what makes it safe to act on, since a PID alone is meaningless once reused, and
-- reap_after_ts is the pass's own deadline plus slack rather than a global age, so a long pass is
-- not reaped for being long. Rows live only as long as the process: written at spawn, deleted when
-- the pass settles, so whatever remains is either running or leaked.

-- Its own table rather than columns on `passes`: a pass row is history worth keeping, while this is
-- bookkeeping about a process that no longer exists once it is answered.
CREATE TABLE pass_processes (
    pass_id       TEXT PRIMARY KEY REFERENCES passes (pass_id) ON DELETE CASCADE,
    pid           INTEGER NOT NULL,
    created_ts    REAL NOT NULL,
    reap_after_ts REAL NOT NULL
);
