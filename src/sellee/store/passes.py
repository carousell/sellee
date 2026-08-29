"""The pass queue: enqueue, claim, finish, and the pass-derived reads."""

from __future__ import annotations

import json

from sellee.db import Database
from sellee.store.helpers import (
    _PASS_TERMINAL,
    _REPLY_THREAD_STATUSES,
    _UNHANDLED_INBOUND_SQL,
    ClaimedPass,
    PassRecord,
    StoreError,
    _claimed_through,
    _insert_notice,
    _new_id,
    _now,
    _unhandled_inbound_rows,
)


class PassesMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

    # --- passes -----------------------------------------------------------------------------

    def enqueue_pass(self, pass_type: str, payload: dict) -> str:
        pass_id = _new_id("pass")
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO passes (pass_id, type, payload, status, requested_ts) "
                "VALUES (?, ?, ?, 'queued', ?)",
                (pass_id, pass_type, json.dumps(payload, sort_keys=True), _now()),
            )
        return pass_id

    def claim_queued_pass(self) -> ClaimedPass | None:
        """Claim the oldest queued pass, stamping it running in the same transaction so two
        claimers never take the same row. Returns None when the queue is empty."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT pass_id, type, payload FROM passes WHERE status = 'queued' "
                "ORDER BY requested_ts ASC, pass_id ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE passes SET status = 'running', started_ts = ? WHERE pass_id = ?",
                (_now(), row["pass_id"]),
            )
            return ClaimedPass(
                pass_id=row["pass_id"], type=row["type"], payload=json.loads(row["payload"])
            )

    def finish_pass(
        self,
        pass_id: str,
        *,
        status: str,
        rc: int | None = None,
        cls: str | None = None,
        summary: str | None = None,
    ) -> None:
        if status not in _PASS_TERMINAL:
            raise StoreError(f"a finished pass status must be one of {_PASS_TERMINAL}")
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE passes SET status = ?, rc = ?, class = ?, summary = ?, finished_ts = ? "
                "WHERE pass_id = ?",
                (status, rc, cls, summary, _now(), pass_id),
            )

    def get_pass(self, pass_id: str) -> PassRecord | None:
        rows = self._db.query("SELECT * FROM passes WHERE pass_id = ?", (pass_id,))
        if not rows:
            return None
        row = rows[0]
        return {
            "pass_id": row["pass_id"],
            "type": row["type"],
            "payload": json.loads(row["payload"]),
            "status": row["status"],
            "rc": row["rc"],
            "class": row["class"],
            "summary": row["summary"],
            "requested_ts": row["requested_ts"],
            "started_ts": row["started_ts"],
            "finished_ts": row["finished_ts"],
        }

    def count_queued_passes(self) -> int:
        rows = self._db.query("SELECT COUNT(*) AS n FROM passes WHERE status = 'queued'")
        return rows[0]["n"]

    def fail_stale_running(self, max_age_sec: float, now: float | None = None) -> list[str]:
        """Fail (never re-run) any pass stuck in `running` past `max_age_sec` — a crash mid-pass.
        Returns the pass_ids failed so the caller can ledger each one loudly."""
        cutoff = (now if now is not None else _now()) - max_age_sec
        with self._db.transaction() as conn:
            stale = conn.execute(
                "SELECT pass_id FROM passes WHERE status = 'running' AND started_ts < ?",
                (cutoff,),
            ).fetchall()
            pass_ids = [r["pass_id"] for r in stale]
            if pass_ids:
                placeholders = ",".join("?" for _ in pass_ids)
                conn.execute(
                    f"UPDATE passes SET status = 'error', class = 'stale', finished_ts = ? "
                    f"WHERE pass_id IN ({placeholders})",
                    (_now(), *pass_ids),
                )
        return pass_ids

    def has_active_channel_pass(self) -> bool:
        """True while a channel pass is queued or running — the coalescing gate: the poller only
        enqueues a new channel pass when this is False, so one pass sweeps all pending rows and
        later arrivals wait for the next (at most one in-flight + one queued batch)."""
        rows = self._db.query(
            "SELECT 1 FROM passes WHERE type = 'channel' "
            "AND status IN ('queued', 'running') LIMIT 1"
        )
        return bool(rows)

    def enqueue_channel_pass(self) -> str | None:
        """Coalescing route, one transaction: when pending inbox rows exist and no channel pass is
        already queued or running, create a channel pass and claim ALL pending rows into it — so
        the lane can never claim the pass before its rows are attached, and one pass sweeps
        everything pending (later arrivals wait for the next). Returns the pass_id, or None when
        there is nothing to do."""
        pass_id = _new_id("pass")
        now = _now()
        with self._db.transaction() as conn:
            active = conn.execute(
                "SELECT 1 FROM passes WHERE type = 'channel' "
                "AND status IN ('queued', 'running') LIMIT 1"
            ).fetchone()
            if active:
                return None
            pending = conn.execute(
                "SELECT id FROM channel_inbox WHERE status = 'pending' ORDER BY id ASC"
            ).fetchall()
            if not pending:
                return None
            ids = [r["id"] for r in pending]
            conn.execute(
                "INSERT INTO passes (pass_id, type, payload, status, requested_ts) "
                "VALUES (?, 'channel', ?, 'queued', ?)",
                (pass_id, json.dumps({"inbox_ids": ids}, sort_keys=True), now),
            )
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE channel_inbox SET status = 'claimed', pass_id = ?, updated_ts = ? "
                f"WHERE id IN ({placeholders})",
                (pass_id, now, *ids),
            )
        return pass_id

    def threads_with_unhandled_inbound(self) -> list[dict]:
        """Sell threads whose buyer is waiting: an inbound message past the reply cursor, a status
        that is still conversational, and no escalation already open on them — an escalation means
        the seller, not the agent, owns the next move."""
        rows = self._db.query(_UNHANDLED_INBOUND_SQL, _REPLY_THREAD_STATUSES)
        return _unhandled_inbound_rows(rows)

    def enqueue_reply_pass(self, skip_markets=()) -> dict | None:
        """Coalescing route for the reply lane, one transaction: claim every sell thread with
        unhandled inbound into a single queued pass, unless one is already queued or running.

        The claimed thread ids (and their owning items) become the pass's payload — the pass token
        is minted with exactly that scope, so the spawned pass can read nothing else. Returns
        {pass_id, thread_ids, item_ids} or None when there is nothing to reply to.

        `skip_markets` holds back the marketplaces whose next send the pacing engine would refuse
        anyway. Filtering here rather than in the caller keeps the claim atomic — a market decided
        sendable and a market actually claimed are the same read — and per-market rather than
        all-or-nothing, because the cap is per marketplace account: one saturated market must never
        mute the buyers waiting on another.
        """
        pass_id = _new_id("pass")
        now = _now()
        skipped = frozenset(skip_markets)
        with self._db.transaction() as conn:
            active = conn.execute(
                "SELECT 1 FROM passes WHERE type = 'reply' "
                "AND status IN ('queued', 'running') LIMIT 1"
            ).fetchone()
            if active:
                return None
            rows = conn.execute(_UNHANDLED_INBOUND_SQL, _REPLY_THREAD_STATUSES).fetchall()
            claimable = [row for row in rows if row["market"] not in skipped]
            pending = _unhandled_inbound_rows(claimable)
            if not pending:
                return None
            thread_ids = [row["thread_id"] for row in pending]
            item_ids = sorted({row["item_id"] for row in pending if row["item_id"]})
            payload = {
                "thread_ids": thread_ids,
                "item_ids": item_ids,
                "claimed_through": _claimed_through(claimable),
            }
            conn.execute(
                "INSERT INTO passes (pass_id, type, payload, status, requested_ts) "
                "VALUES (?, 'reply', ?, 'queued', ?)",
                (pass_id, json.dumps(payload, sort_keys=True), now),
            )
        return {"pass_id": pass_id, **payload}

    def last_finished_pass(self, pass_type: str) -> dict | None:
        """The most recently finished pass of a type, as {class, finished_ts} — or None.

        The reply lane's cooldown read: a pass that ended `no_send` is the one signal that
        respawning right now would only repeat it.
        """
        rows = self._db.query(
            "SELECT class, finished_ts FROM passes WHERE type = ? AND finished_ts IS NOT NULL "
            "ORDER BY finished_ts DESC, pass_id DESC LIMIT 1",
            (pass_type,),
        )
        if not rows:
            return None
        return {"class": rows[0]["class"], "finished_ts": rows[0]["finished_ts"]}

    def active_passes_of_types(self, types) -> list[dict]:
        """Queued or running passes of the given types, as {type, payload}.

        The inbox lane reads this at tick start to decide whether to yield the browser: which of
        these passes actually drives Chrome depends on the marketplace's connector, which is the
        browser layer's business, not the store's — so this hands back the rows and lets the caller
        judge rather than encoding market knowledge here.
        """
        types = tuple(types)
        if not types:
            return []
        placeholders = ",".join("?" for _ in types)
        rows = self._db.query(
            f"SELECT type, payload FROM passes WHERE type IN ({placeholders}) "
            "AND status IN ('queued', 'running')",
            types,
        )
        return [{"type": r["type"], "payload": json.loads(r["payload"])} for r in rows]

    def publish_pass_index(self) -> list[dict]:
        """Every publish pass ever queued, as {market, item_id, status, origin}.

        The fan-out's whole memory of what it has tried. A publish is attempted once per item and
        marketplace: rows are never pruned, so the history is the attempt counter and there is no
        second piece of state to keep in step with it. Markets are decided by the caller — the store
        holds no marketplace knowledge.
        """
        rows = self._db.query("SELECT payload, status FROM passes WHERE type = 'publish'")
        out = []
        for row in rows:
            payload = json.loads(row["payload"])
            out.append(
                {
                    "market": payload.get("market"),
                    "item_id": payload.get("item_id"),
                    "origin": payload.get("origin"),
                    "status": row["status"],
                }
            )
        return out

    def unreported_crosslist_passes(self) -> list[dict]:
        """Settled fan-out publishes the seller has not been told about, oldest first.

        Only the ones the daemon started: a publish run from the CLI is watched by whoever ran it.
        Those rows owe no report, so their flag is closed the first time the sweep sees them —
        `reported` means "no report owed" (the meaning the migration's backfill established), and
        closing it keeps this scan bounded by work owed rather than by CLI history.
        """
        rows = self._db.query(
            "SELECT pass_id, payload, status, class FROM passes "
            "WHERE type = 'publish' AND reported = 0 AND status IN ('done', 'error') "
            "ORDER BY finished_ts ASC, pass_id ASC"
        )
        out = []
        owes_nothing = []
        for row in rows:
            payload = json.loads(row["payload"])
            if payload.get("origin") != "crosslist":
                owes_nothing.append((row["pass_id"],))
                continue
            out.append(
                {
                    "pass_id": row["pass_id"],
                    "item_id": payload.get("item_id"),
                    "market": payload.get("market"),
                    "status": row["status"],
                    "class": row["class"],
                }
            )
        if owes_nothing:
            with self._db.transaction() as conn:
                conn.executemany("UPDATE passes SET reported = 1 WHERE pass_id = ?", owes_nothing)
        return out

    def report_crosslist_pass(self, pass_id: str, text: str, *, ref: str | None = None) -> bool:
        """Tell the seller how a fan-out publish went and flag the pass, or do neither.

        One transaction, and the flag is only cleared-to-set once, so a crash mid-sweep cannot
        announce a listing twice or swallow the announcement. Returns whether this call was the one
        that reported it.
        """
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE passes SET reported = 1 WHERE pass_id = ? AND reported = 0", (pass_id,)
            )
            if cur.rowcount == 0:
                return False
            _insert_notice(conn, text, ref=ref)
        return True

    def sold_item_ids(self) -> set:
        """Items whose sale is settled. Sale state lives in the negotiation ledger, not on the item,
        so this is the one honest answer to "is this item still for sale"."""
        rows = self._db.query("SELECT item_id FROM negotiations WHERE state = 'sold'")
        return {row["item_id"] for row in rows}

    def crosslink_pushed_urls(self) -> dict:
        """The last external-URL set the rail accepted, per item: {item_id: canonical JSON}. An
        item with no row has never had a set accepted."""
        rows = self._db.query("SELECT item_id, pushed_urls FROM crosslink_pushes")
        return {row["item_id"]: row["pushed_urls"] for row in rows}

    def set_crosslink_pushed(self, item_id: str, urls_json: str) -> None:
        """Record the set the rail just accepted. Written only after the rail call succeeds — a
        marker ahead of acceptance would silence the retry that makes the push reliable."""
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO crosslink_pushes (item_id, pushed_urls, pushed_ts) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT (item_id) DO UPDATE SET "
                "pushed_urls = excluded.pushed_urls, pushed_ts = excluded.pushed_ts",
                (item_id, urls_json, _now()),
            )
