"""Buy-side wants and their budgets."""

from __future__ import annotations

import json

from sellee.db import Database
from sellee.store import (
    BudgetAck,
    BudgetRecord,
    StoreError,
    WantNotFound,
    WantRecord,
    _new_id,
    _now,
    _want_from_row,
)


class WantsMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

    # --- wants ------------------------------------------------------------------------------

    def create_want(
        self,
        *,
        query: str,
        category: str | None = None,
        condition_pref: str | None = None,
        region: str | None = None,
        currency: str | None = None,
        target_price: float | None = None,
        source: str | None = None,
    ) -> WantRecord:
        if not query or not query.strip():
            raise StoreError("query must be non-empty")
        want_id = _new_id("want")
        ts = _now()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO wants (want_id, query, category, condition_pref, region, currency, "
                "target_price, status, source, candidates, shortlist, created_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'searching', ?, '[]', '[]', ?, ?)",
                (
                    want_id,
                    query.strip(),
                    category,
                    condition_pref,
                    region,
                    currency,
                    target_price,
                    source,
                    ts,
                    ts,
                ),
            )
        return self.get_want(want_id)  # type: ignore[return-value]

    def get_want(self, want_id: str) -> WantRecord | None:
        rows = self._db.query("SELECT * FROM wants WHERE want_id = ?", (want_id,))
        return _want_from_row(rows[0]) if rows else None

    def list_wants(self, status: str | None = None) -> list[WantRecord]:
        if status is None:
            rows = self._db.query("SELECT * FROM wants ORDER BY created_ts DESC")
        else:
            rows = self._db.query(
                "SELECT * FROM wants WHERE status = ? ORDER BY created_ts DESC", (status,)
            )
        return [_want_from_row(r) for r in rows]

    _WANT_WRITABLE = (
        "query",
        "category",
        "condition_pref",
        "region",
        "currency",
        "target_price",
        "candidates",
        "shortlist",
    )
    _WANT_JSON_FIELDS = ("candidates", "shortlist")
    # Thread states a want-cancel closes; closed/escalated threads are left as they are.
    _WANT_OPEN_THREAD_STATUSES = ("active", "liaising", "agreed", "held")

    def update_want(self, want_id: str, fields: dict) -> WantRecord:
        unknown = [k for k in fields if k not in self._WANT_WRITABLE]
        if unknown:
            raise StoreError(
                f"unknown or non-writable want field(s): {', '.join(sorted(unknown))}; "
                f"writable: {', '.join(self._WANT_WRITABLE)} (cancelled is owned by cancel_want, "
                "bought by the buy close flow)"
            )
        if not fields:
            raise StoreError("no fields to update")
        assignments = {
            k: (json.dumps(v) if k in self._WANT_JSON_FIELDS else v) for k, v in fields.items()
        }
        clause = ", ".join(f"{name} = ?" for name in assignments)
        with self._db.transaction() as conn:
            if not conn.execute("SELECT 1 FROM wants WHERE want_id = ?", (want_id,)).fetchone():
                raise WantNotFound(f"no want with id {want_id!r}")
            conn.execute(
                f"UPDATE wants SET {clause}, updated_ts = ? WHERE want_id = ?",
                (*assignments.values(), _now(), want_id),
            )
        return self.get_want(want_id)  # type: ignore[return-value]

    def cancel_want(self, want_id: str, reason: str | None = None) -> dict:
        """Cancel a want and close its open buy threads in one transaction. Idempotent: an
        already-terminal want is left as-is (but stray open threads are still swept). Refuses a
        bought want — that is a completed purchase, not something to cancel. Never touches the
        budget."""
        with self._db.transaction() as conn:
            row = conn.execute("SELECT status FROM wants WHERE want_id = ?", (want_id,)).fetchone()
            if not row:
                raise WantNotFound(f"no want with id {want_id!r}")
            if row["status"] == "bought":
                raise StoreError(f"want {want_id!r} is already bought — nothing to cancel")
            ts = _now()
            if row["status"] not in ("cancelled", "abandoned"):
                conn.execute(
                    "UPDATE wants SET status = 'cancelled', cancelled_ts = ?, cancel_reason = ?, "
                    "updated_ts = ? WHERE want_id = ?",
                    (ts, reason, ts, want_id),
                )
            placeholders = ",".join("?" for _ in self._WANT_OPEN_THREAD_STATUSES)
            open_threads = conn.execute(
                f"SELECT thread_id FROM threads WHERE want_id = ? AND side = 'buy' "
                f"AND status IN ({placeholders})",
                (want_id, *self._WANT_OPEN_THREAD_STATUSES),
            ).fetchall()
            closed = [t["thread_id"] for t in open_threads]
            for thread_id in closed:
                conn.execute(
                    "UPDATE threads SET status = 'closed', closed_ts = ?, "
                    "closed_reason = ?, updated_ts = ? WHERE thread_id = ?",
                    (ts, "want cancelled", ts, thread_id),
                )
        return {"want_id": want_id, "status": "cancelled", "threads_closed": closed}

    # --- budgets ----------------------------------------------------------------------------

    _BUDGET_SOURCES = ("buyer", "default")

    def set_budget(
        self,
        want_id: str,
        max_budget: float,
        source: str,
        *,
        target_price: float | None = None,
        currency: str | None = None,
        force: bool = False,
        opening_ratio: float | None = None,
        auto_counter_step: int | None = None,
        auto_counter_rounds: int | None = None,
        give_up_polls: int | None = None,
    ) -> BudgetAck:
        """The one hardened budget writer — the buy-side mirror of set_floor, closing the legacy
        free-write hole. Validates 0 < target <= max, records provenance, refuses a `default` write
        over a `buyer` value (force replaces a buyer value), and never echoes a value."""
        if source not in self._BUDGET_SOURCES:
            raise StoreError(f"source must be one of {self._BUDGET_SOURCES}, got {source!r}")
        if (
            not isinstance(max_budget, (int, float))
            or isinstance(max_budget, bool)
            or max_budget <= 0
        ):
            raise StoreError("max_budget must be a positive number")
        target = target_price if target_price is not None else max_budget
        if not isinstance(target, (int, float)) or isinstance(target, bool) or target <= 0:
            raise StoreError("target_price must be a positive number")
        if target > max_budget:
            raise StoreError("target_price must be at or below max_budget")
        with self._db.transaction() as conn:
            if not conn.execute("SELECT 1 FROM wants WHERE want_id = ?", (want_id,)).fetchone():
                raise WantNotFound(f"no want with id {want_id!r}")
            existing = conn.execute(
                "SELECT source FROM budgets WHERE want_id = ?", (want_id,)
            ).fetchone()
            replaced = existing["source"] if existing else None
            if replaced == "buyer" and not (source == "buyer" and force):
                raise StoreError(
                    "a buyer-set budget already exists for this want — refusing to overwrite "
                    "(an explicit buyer correction with force is required to change it)"
                )
            conn.execute(
                "INSERT INTO budgets "
                "(want_id, max_budget, target_price, currency, opening_ratio, auto_counter_step, "
                " auto_counter_rounds, give_up_polls, source, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (want_id) DO UPDATE SET "
                "max_budget = excluded.max_budget, target_price = excluded.target_price, "
                "currency = excluded.currency, opening_ratio = excluded.opening_ratio, "
                "auto_counter_step = excluded.auto_counter_step, "
                "auto_counter_rounds = excluded.auto_counter_rounds, "
                "give_up_polls = excluded.give_up_polls, source = excluded.source, "
                "updated_ts = excluded.updated_ts",
                (
                    want_id,
                    max_budget,
                    target,
                    currency,
                    opening_ratio,
                    auto_counter_step,
                    auto_counter_rounds,
                    give_up_polls,
                    source,
                    _now(),
                ),
            )
        return {"status": "written", "want_id": want_id, "source": source, "replaced": replaced}

    def get_budget(self, want_id: str) -> BudgetRecord | None:
        """Internal only: the confidential budget record. No LLM-facing tool calls this — only
        the buyer engine loads it, exactly as get_floor serves the sell engine."""
        rows = self._db.query("SELECT * FROM budgets WHERE want_id = ?", (want_id,))
        if not rows:
            return None
        row = rows[0]
        return {
            "want_id": row["want_id"],
            "max_budget": row["max_budget"],
            "target_price": row["target_price"],
            "currency": row["currency"],
            "opening_ratio": row["opening_ratio"],
            "auto_counter_step": row["auto_counter_step"],
            "auto_counter_rounds": row["auto_counter_rounds"],
            "give_up_polls": row["give_up_polls"],
            "source": row["source"],
            "updated_ts": row["updated_ts"],
        }
