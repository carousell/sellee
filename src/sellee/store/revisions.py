"""The queue of edits still owed to a browser marketplace, and how each one is reported.

The rail is edited inline, in the turn that asks for it. A browser marketplace cannot be: driving
a logged-in account takes minutes, so the ask and the doing are separated by a durable row, and
everything that makes that safe lives here — supersede rather than stack, claim single-flight,
bound the attempts, and let the daemon tell the seller from the row rather than from a model's
memory of what it meant to say.

Deliberately not reusing the fan-out's reporting: `unreported_crosslist_passes` is filtered in SQL
to `type = 'publish'` and then to `origin = 'crosslist'`, so an edit would be invisible to it.
Widening that scan to serve two callers would make one lane's bug the other's; the shape is copied
instead, which is cheap and keeps each lane's reporting its own.
"""

from __future__ import annotations

import json

from sellee.db import Database
from sellee.store.helpers import (
    ItemNotFound,
    RevisionRecord,
    StoreError,
    _insert_notice,
    _new_id,
    _now,
)

# What a revision row may be. `superseded` is not a failure: the seller asked for something else
# before this one ran, and the newer row carries the whole intent because the lane re-reads the
# item rather than a snapshot.
_REVISION_STATUSES = ("pending", "running", "done", "failed", "superseded")
_REVISION_TERMINAL = ("done", "failed")


def _revision_from_row(row) -> RevisionRecord:
    return {
        "revision_id": row["revision_id"],
        "item_id": row["item_id"],
        "market": row["market"],
        "changed": json.loads(row["changed"]),
        "accepted": json.loads(row["accepted"]) if row["accepted"] else None,
        "status": row["status"],
        "attempts": row["attempts"],
        "last_error": row["last_error"],
        "claimed_ts": row["claimed_ts"],
        "pass_id": row["pass_id"],
        "created_ts": row["created_ts"],
        "finished_ts": row["finished_ts"],
    }


def _next_pending_query(due_by: float) -> tuple:
    return (
        "SELECT * FROM listing_revisions WHERE status = 'pending' "
        "AND (claimed_ts IS NULL OR claimed_ts <= ?) "
        "ORDER BY created_ts ASC, revision_id ASC LIMIT 1",
        (due_by,),
    )


class RevisionsMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

    def queue_listing_revision(self, item_id: str, market: str, changed) -> str:
        """Owe this marketplace an edit, replacing any edit it was already owed.

        Superseding rather than queueing a second row is the whole point: a seller who drops a
        price and then drops it again has asked for one listing at the second number, not two
        drives minutes apart. The surviving row names the union of what changed, because the
        earlier row's fields are still divergent on that marketplace — dropping them would leave a
        description edit unpushed because a price edit followed it.

        A row already running is not superseded — it is mid-drive — but its fields are carried
        too. It may yet fail, and the new row is then the one that must still push them; pushing
        a field the running edit did land is harmless, because an edit is idempotent.
        """
        changed = sorted({str(name) for name in changed})
        if not changed:
            raise StoreError("a revision must name at least one changed field")
        revision_id = _new_id("rev")
        now = _now()
        with self._db.transaction() as conn:
            if not conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
                raise ItemNotFound(f"no item with id {item_id!r}")
            pending = conn.execute(
                "SELECT revision_id, changed FROM listing_revisions "
                "WHERE item_id = ? AND market = ? AND status = 'pending'",
                (item_id, market),
            ).fetchall()
            running = conn.execute(
                "SELECT changed FROM listing_revisions "
                "WHERE item_id = ? AND market = ? AND status = 'running'",
                (item_id, market),
            ).fetchall()
            carried = set(changed)
            for row in list(pending) + list(running):
                carried.update(json.loads(row["changed"]))
            if pending:
                conn.execute(
                    "UPDATE listing_revisions SET status = 'superseded', finished_ts = ? "
                    "WHERE item_id = ? AND market = ? AND status = 'pending'",
                    (now, item_id, market),
                )
            conn.execute(
                "INSERT INTO listing_revisions "
                "(revision_id, item_id, market, changed, status, created_ts) "
                "VALUES (?, ?, ?, ?, 'pending', ?)",
                (revision_id, item_id, market, json.dumps(sorted(carried)), now),
            )
        return revision_id

    def list_listing_revisions(self, status: str | None = None) -> list[RevisionRecord]:
        """Revision rows, oldest first. The lane's view of what is owed and what has settled."""
        if status is None:
            rows = self._db.query(
                "SELECT * FROM listing_revisions ORDER BY created_ts ASC, revision_id ASC"
            )
        else:
            rows = self._db.query(
                "SELECT * FROM listing_revisions WHERE status = ? "
                "ORDER BY created_ts ASC, revision_id ASC",
                (status,),
            )
        return [_revision_from_row(row) for row in rows]

    def revision_in_flight(self) -> bool:
        """Whether an edit is already claimed. Passes run one at a time and the browser is one
        shared tab, so a second claim only lengthens the wait ahead of the seller's own chat."""
        rows = self._db.query(
            "SELECT 1 FROM listing_revisions WHERE status = 'running' LIMIT 1",
        )
        return bool(rows)

    def next_listing_revision(self, *, retry_after_sec: float = 0.0) -> RevisionRecord | None:
        """The revision the next claim would take, without claiming it — so the lane can ask
        whether it could run at all before spending one of the row's attempts on finding out."""
        rows = self._db.query(*_next_pending_query(_now() - retry_after_sec))
        return _revision_from_row(rows[0]) if rows else None

    def claim_listing_revision(self, *, retry_after_sec: float = 0.0) -> RevisionRecord | None:
        """Claim the oldest pending revision that is due, stamping it running in one transaction.

        Single-flight by construction, exactly as `claim_queued_pass` is: two claimers can never
        take the same row, so a lane tick that overlaps its predecessor cannot drive one edit
        twice. A row handed back for another go is not due until `retry_after_sec` after its last
        attempt started.
        """
        now = _now()
        with self._db.transaction() as conn:
            row = conn.execute(*_next_pending_query(now - retry_after_sec)).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE listing_revisions SET status = 'running', attempts = attempts + 1, "
                "claimed_ts = ? WHERE revision_id = ?",
                (now, row["revision_id"]),
            )
            claimed = _revision_from_row(row)
        claimed["status"] = "running"
        claimed["attempts"] = row["attempts"] + 1
        claimed["claimed_ts"] = now
        return claimed

    def attach_revision_pass(self, revision_id: str, pass_id: str) -> None:
        """Record which edit pass is driving a claimed revision, so its end can be recognised."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE listing_revisions SET pass_id = ? WHERE revision_id = ?",
                (pass_id, revision_id),
            )

    def get_listing_revision(self, revision_id: str) -> RevisionRecord | None:
        rows = self._db.query(
            "SELECT * FROM listing_revisions WHERE revision_id = ?", (revision_id,)
        )
        return _revision_from_row(rows[0]) if rows else None

    def finish_listing_revision(
        self,
        revision_id: str,
        *,
        status: str,
        accepted: dict | None = None,
        error: str | None = None,
        retry: bool = False,
    ) -> RevisionRecord | None:
        """Settle a claimed revision, or hand it back for another go.

        `retry=True` returns it to `pending` instead of settling it — for a refusal that says
        nothing about the next attempt (the browser was busy, a wall went up). The attempt is
        already spent by the claim, so the lane's bound holds either way and a row cannot loop
        forever on a condition that never clears.
        """
        if not retry and status not in _REVISION_TERMINAL:
            raise StoreError(f"a settled revision must be one of {_REVISION_TERMINAL}")
        now = _now()
        with self._db.transaction() as conn:
            if retry:
                conn.execute(
                    "UPDATE listing_revisions SET status = 'pending', last_error = ? "
                    "WHERE revision_id = ? AND status = 'running'",
                    (error, revision_id),
                )
            else:
                conn.execute(
                    "UPDATE listing_revisions SET status = ?, accepted = ?, last_error = ?, "
                    "finished_ts = ? WHERE revision_id = ? AND status = 'running'",
                    (
                        status,
                        json.dumps(accepted, sort_keys=True) if accepted is not None else None,
                        error,
                        now,
                        revision_id,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM listing_revisions WHERE revision_id = ?", (revision_id,)
            ).fetchone()
        return _revision_from_row(row) if row else None

    def unreported_listing_revisions(self) -> list[RevisionRecord]:
        """Settled edits the seller has not been told about, oldest first.

        `superseded` rows are settled but owe nothing — the seller asked for something else and
        the newer row will speak for both. Their flag is closed here so the scan stays bounded by
        work owed rather than by history, the same trick the fan-out's sweep uses for a publish
        somebody ran from the CLI.
        """
        rows = self._db.query(
            "SELECT * FROM listing_revisions WHERE reported = 0 AND status IN "
            "('done', 'failed', 'superseded') ORDER BY finished_ts ASC, revision_id ASC"
        )
        owes_nothing = [(row["revision_id"],) for row in rows if row["status"] == "superseded"]
        if owes_nothing:
            with self._db.transaction() as conn:
                conn.executemany(
                    "UPDATE listing_revisions SET reported = 1 WHERE revision_id = ?",
                    owes_nothing,
                )
        return [_revision_from_row(row) for row in rows if row["status"] != "superseded"]

    def report_listing_revision(
        self, revision_id: str, text: str | None, *, ref: str | None = None
    ) -> bool:
        """Tell the seller how one marketplace's edit went, and flag the row — or do neither.

        One transaction, and the flag only moves once, so a crash mid-sweep can neither announce
        an edit twice nor swallow the announcement. `text=None` closes the row silently, for a
        failure the lane still intends to retry.

        The notice carries no `pass_id`. One that did would be read as a channel pass having
        answered the seller, and a background report is not an answer to anything they asked in
        that turn.
        """
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE listing_revisions SET reported = 1 WHERE revision_id = ? AND reported = 0",
                (revision_id,),
            )
            if cur.rowcount == 0:
                return False
            if text is not None:
                _insert_notice(conn, text, ref=ref)
        return True
