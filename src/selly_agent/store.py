"""Typed accessors over the business database — the one writer for items, floors, and passes.

Every state change the LLM can cause lands here as an explicit function on the single write
connection, in a real transaction. Two disciplines are load-bearing:

  * The floor is confidential. It lives in its own table and is never returned by a read an
    LLM-facing tool can call — only the publish gate and (later) the engines load it. set_floor
    is the one hardened writer: it validates 0 < floor <= list_price (list price from the item
    record, never the caller), records provenance, refuses to let a `default` write clobber a
    seller value, requires force to replace one seller value with another, and never emits the
    value. The check and the write share one transaction so a race can't clobber a just-set
    seller floor with a default.

  * update_item is field-constrained: transcript-style fields don't exist here, listing_urls is
    written only by the publish path (never a hand-edit), and status moves only between draft and
    ready — the sale-state transitions belong to their owning flow, not a generic writer.

Passes are claimed single-flight: claim_queued_pass stamps running + started_ts in one
transaction, so two claimers never take the same row; a crash mid-pass is failed loudly by the
stale-running sweep, never silently re-run.
"""

from __future__ import annotations

import inspect
import json
import time
import uuid
from dataclasses import dataclass

from . import marketplaces
from .db import Database
from .engines import buyer_negotiate as buyer_engine
from .engines import negotiate as negotiate_engine
from .engines import pacing as pacing_engine
from .engines import scam as scam_engine

# Fields a caller may set on an item. listing_urls is deliberately absent — it is written only
# after a live listing verify, by the publish path. Sale-state transitions are not here either.
_ITEM_WRITABLE = (
    "title",
    "description",
    "condition",
    "list_price",
    "currency",
    "status",
    "size_bucket",
)
_ITEM_STATUSES = ("draft", "ready")

_FLOOR_SOURCES = ("seller", "default")

_PASS_TERMINAL = ("done", "error")

_THREAD_SIDES = ("sell", "buy")
# Thread transcript reads are capped so a huge thread can't blow a pass's context.
_TRANSCRIPT_DEFAULT_CAP = 100


class StoreError(Exception):
    """An expected, caller-facing store failure (bad input, not found, refused overwrite).

    Tools translate this into a structured tool error; it never carries a secret value.
    """


class ItemNotFound(StoreError):
    pass


class ThreadNotFound(StoreError):
    pass


class WantNotFound(StoreError):
    pass


@dataclass(frozen=True)
class ClaimedPass:
    pass_id: str
    type: str
    payload: dict


def _now() -> float:
    return time.time()


def _load_scam_registry() -> tuple:
    """Load the packaged scam registry. Returns (signatures, ok); an unreadable or malformed file
    degrades to bank-only (ok False) rather than trusting a poisoned registry — the full schema
    check runs as a CI guard over the shipped file, not at runtime."""
    try:
        doc = json.loads(marketplaces.SCAM_REGISTRY_PATH.read_text())
    except (OSError, ValueError):
        return [], False
    sigs = doc.get("signatures") if isinstance(doc, dict) else None
    if not isinstance(sigs, list):
        return [], False
    return sigs, True


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _item_from_row(row) -> dict:
    """The buyer-safe item view — never a floor."""
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "condition": row["condition"],
        "list_price": row["list_price"],
        "currency": row["currency"],
        "status": row["status"],
        "size_bucket": row["size_bucket"],
        "listing_urls": json.loads(row["listing_urls"]),
        "created_ts": row["created_ts"],
        "updated_ts": row["updated_ts"],
    }


_THREAD_FIELDS = (
    "thread_id",
    "side",
    "market",
    "item_id",
    "want_id",
    "counterpart_handle",
    "status",
    "held_reason",
    "held_from_status",
    "buyer_location",
    "agent_note",
    "listing_url",
    "listed_price",
    "close_method",
    "closed_ts",
    "closed_reason",
    "cursor_last_msg_id",
    "cursor_last_ts",
    "last_followup_ts",
    "followup_disposition",
    "source",
    "created_ts",
    "updated_ts",
)


def _thread_from_row(row) -> dict:
    """The thread record — identity, status, cursor, side-specific fields. Never a floor/budget."""
    return {name: row[name] for name in _THREAD_FIELDS}


_WANT_FIELDS = (
    "want_id",
    "query",
    "category",
    "condition_pref",
    "region",
    "currency",
    "target_price",
    "status",
    "source",
    "cancelled_ts",
    "cancel_reason",
    "created_ts",
    "updated_ts",
)


def _want_from_row(row) -> dict:
    """The buyer-safe want view — never the max budget (that lives only behind the engine)."""
    record = {name: row[name] for name in _WANT_FIELDS}
    record["candidates"] = json.loads(row["candidates"])
    record["shortlist"] = json.loads(row["shortlist"])
    return record


class Store:
    """Typed access to selly.db, serialized behind the single write connection."""

    def __init__(self, db: Database):
        self._db = db

    # --- items: reads -----------------------------------------------------------------------

    def get_item(self, item_id: str) -> dict | None:
        rows = self._db.query("SELECT * FROM items WHERE id = ?", (item_id,))
        return _item_from_row(rows[0]) if rows else None

    def list_items(self, status: str | None = None) -> list[dict]:
        if status is None:
            rows = self._db.query("SELECT * FROM items ORDER BY created_ts DESC")
        else:
            rows = self._db.query(
                "SELECT * FROM items WHERE status = ? ORDER BY created_ts DESC", (status,)
            )
        return [_item_from_row(r) for r in rows]

    # --- items: writes ----------------------------------------------------------------------

    def create_item(
        self,
        *,
        title: str,
        list_price: float,
        currency: str | None = None,
        description: str = "",
        condition: str | None = None,
    ) -> dict:
        if not title or not title.strip():
            raise StoreError("title must be non-empty")
        item_id = _new_id("item")
        ts = _now()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO items "
                "(id, title, description, condition, list_price, currency, status, "
                " listing_urls, created_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, 'draft', '{}', ?, ?)",
                (
                    item_id,
                    title.strip(),
                    description or "",
                    condition,
                    list_price,
                    currency,
                    ts,
                    ts,
                ),
            )
        return self.get_item(item_id)  # type: ignore[return-value]

    def update_item(self, item_id: str, fields: dict) -> dict:
        if "listing_urls" in fields:
            raise StoreError(
                "listing_urls is not writable here — it is recorded by "
                "carousell_ai_publish_listing after the listing is verified live"
            )
        unknown = [k for k in fields if k not in _ITEM_WRITABLE]
        if unknown:
            raise StoreError(
                f"unknown or non-writable field(s): {', '.join(sorted(unknown))}; "
                f"writable: {', '.join(_ITEM_WRITABLE)}"
            )
        if "status" in fields and fields["status"] not in _ITEM_STATUSES:
            raise StoreError(
                f"status may only move between {_ITEM_STATUSES}; sale-state transitions are "
                "owned by their own flow"
            )
        if not fields:
            raise StoreError("no fields to update")

        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [fields[name] for name in fields]
        with self._db.transaction() as conn:
            exists = conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone()
            if not exists:
                raise ItemNotFound(f"no item with id {item_id!r}")
            conn.execute(
                f"UPDATE items SET {assignments}, updated_ts = ? WHERE id = ?",
                (*values, _now(), item_id),
            )
        return self.get_item(item_id)  # type: ignore[return-value]

    def record_listing_url(self, item_id: str, market: str, url: str) -> dict:
        """Merge one verified listing URL into the item's listing_urls map. The one writer of
        that field — a live verify has already passed before this is called."""
        with self._db.transaction() as conn:
            row = conn.execute("SELECT listing_urls FROM items WHERE id = ?", (item_id,)).fetchone()
            if not row:
                raise ItemNotFound(f"no item with id {item_id!r}")
            urls = json.loads(row["listing_urls"])
            urls[market] = url
            conn.execute(
                "UPDATE items SET listing_urls = ?, updated_ts = ? WHERE id = ?",
                (json.dumps(urls, sort_keys=True), _now(), item_id),
            )
        return self.get_item(item_id)  # type: ignore[return-value]

    # --- floors -----------------------------------------------------------------------------

    def get_floor(self, item_id: str) -> dict | None:
        """Internal only: the confidential floor record. No LLM-facing tool calls this."""
        rows = self._db.query("SELECT * FROM floors WHERE item_id = ?", (item_id,))
        if not rows:
            return None
        row = rows[0]
        return {
            "item_id": row["item_id"],
            "floor": row["floor"],
            "currency": row["currency"],
            "source": row["source"],
            "auto_counter_step": row["auto_counter_step"],
            "auto_counter_rounds": row["auto_counter_rounds"],
            "updated_ts": row["updated_ts"],
        }

    def set_floor(
        self,
        item_id: str,
        floor: float,
        source: str,
        force: bool = False,
        *,
        auto_counter_step: int | None = None,
        auto_counter_rounds: int | None = None,
    ) -> dict:
        """The one hardened floor writer. Returns an ack carrying provenance only — never the
        value. Raises StoreError on invalid input or a refused overwrite."""
        if source not in _FLOOR_SOURCES:
            raise StoreError(f"source must be one of {_FLOOR_SOURCES}, got {source!r}")
        if not isinstance(floor, (int, float)) or isinstance(floor, bool) or floor <= 0:
            raise StoreError("floor must be a positive number")
        with self._db.transaction() as conn:
            item = conn.execute(
                "SELECT list_price, currency FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if not item:
                raise ItemNotFound(f"no item with id {item_id!r}")
            list_price = item["list_price"]
            if not isinstance(list_price, (int, float)) or list_price <= 0:
                raise StoreError(f"item {item_id!r} has no valid list price to bound the floor")
            if floor > list_price:
                raise StoreError(
                    "floor is above the list price — lower the floor or raise the "
                    "listing price first"
                )
            existing = conn.execute(
                "SELECT source FROM floors WHERE item_id = ?", (item_id,)
            ).fetchone()
            replaced = existing["source"] if existing else None
            if replaced == "seller" and not (source == "seller" and force):
                raise StoreError(
                    "a seller-set floor already exists for this item — refusing to overwrite "
                    "(an explicit seller correction with force is required to change it)"
                )
            conn.execute(
                "INSERT INTO floors "
                "(item_id, floor, currency, source, auto_counter_step, auto_counter_rounds, "
                " updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (item_id) DO UPDATE SET "
                "floor = excluded.floor, currency = excluded.currency, "
                "source = excluded.source, auto_counter_step = excluded.auto_counter_step, "
                "auto_counter_rounds = excluded.auto_counter_rounds, "
                "updated_ts = excluded.updated_ts",
                (
                    item_id,
                    floor,
                    item["currency"],
                    source,
                    auto_counter_step,
                    auto_counter_rounds,
                    _now(),
                ),
            )
        return {"status": "written", "item_id": item_id, "source": source, "replaced": replaced}

    # --- threads ----------------------------------------------------------------------------

    def create_thread(
        self,
        *,
        thread_id: str,
        side: str,
        market: str,
        counterpart_handle: str,
        item_id: str | None = None,
        want_id: str | None = None,
        listing_url: str | None = None,
        listed_price: float | None = None,
        buyer_location: str | None = None,
        source: str | None = None,
    ) -> dict:
        """Create an identity-complete thread. The natural key is the caller-supplied
        `<market>:<local id>`; identity (side, market, counterpart, and the owning item/want) is
        required at creation so the suppression layers that key off it can never be disabled by a
        skeleton thread."""
        if side not in _THREAD_SIDES:
            raise StoreError(f"side must be one of {_THREAD_SIDES}, got {side!r}")
        if not market or not market.strip():
            raise StoreError("market must be non-empty")
        if not thread_id or not thread_id.startswith(f"{market}:"):
            raise StoreError(f"thread_id must start with {market!r} + ':' (got {thread_id!r})")
        if not counterpart_handle or not counterpart_handle.strip():
            raise StoreError("counterpart_handle must be non-empty")
        if side == "sell" and not item_id:
            raise StoreError("a sell thread requires item_id")
        if side == "buy" and not want_id:
            raise StoreError("a buy thread requires want_id")
        ts = _now()
        with self._db.transaction() as conn:
            if conn.execute("SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)).fetchone():
                raise StoreError(f"thread {thread_id!r} already exists")
            if (
                item_id
                and not conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone()
            ):
                raise ItemNotFound(f"no item with id {item_id!r}")
            if (
                want_id
                and not conn.execute("SELECT 1 FROM wants WHERE want_id = ?", (want_id,)).fetchone()
            ):
                raise WantNotFound(f"no want with id {want_id!r}")
            conn.execute(
                "INSERT INTO threads (thread_id, side, market, item_id, want_id, "
                "counterpart_handle, status, listing_url, listed_price, buyer_location, source, "
                "created_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    side,
                    market.strip(),
                    item_id,
                    want_id,
                    counterpart_handle.strip(),
                    listing_url,
                    listed_price,
                    buyer_location,
                    source,
                    ts,
                    ts,
                ),
            )
        return self.get_thread(thread_id)  # type: ignore[return-value]

    def get_thread(
        self, thread_id: str, *, message_cap: int = _TRANSCRIPT_DEFAULT_CAP
    ) -> dict | None:
        rows = self._db.query("SELECT * FROM threads WHERE thread_id = ?", (thread_id,))
        if not rows:
            return None
        record = _thread_from_row(rows[0])
        record["messages"] = self.get_thread_messages(thread_id, limit=message_cap)
        record["message_count"] = self._db.query(
            "SELECT COUNT(*) AS n FROM thread_messages WHERE thread_id = ?", (thread_id,)
        )[0]["n"]
        return record

    def list_threads(self, side: str | None = None, status: str | None = None) -> list[dict]:
        clauses, params = [], []
        if side is not None:
            clauses.append("side = ?")
            params.append(side)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM threads"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_ts DESC"
        rows = self._db.query(sql, tuple(params))
        return [_thread_from_row(r) for r in rows]

    def append_thread_message(
        self,
        thread_id: str,
        *,
        msg_id: str,
        direction: str,
        text: str,
        ts: float | None = None,
        source: str | None = None,
    ) -> bool:
        """Fold one transcript row into a thread, deduped by (thread_id, msg_id). Returns True if
        it was newly inserted, False if the constraint dropped a duplicate — dedup by the schema,
        never by a read-then-write race."""
        if direction not in ("in", "out"):
            raise StoreError("direction must be 'in' or 'out'")
        with self._db.transaction() as conn:
            if not conn.execute(
                "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone():
                raise ThreadNotFound(f"no thread with id {thread_id!r}")
            cur = conn.execute(
                "INSERT OR IGNORE INTO thread_messages "
                "(thread_id, msg_id, dir, text, ts, source) VALUES (?, ?, ?, ?, ?, ?)",
                (thread_id, msg_id, direction, text, ts if ts is not None else _now(), source),
            )
            return cur.rowcount > 0

    def get_thread_messages(self, thread_id: str, *, limit: int | None = None) -> list[dict]:
        """The transcript in chronological order, capped to the most recent `limit` rows."""
        if limit is None:
            rows = self._db.query(
                "SELECT msg_id, dir, text, ts, source FROM thread_messages "
                "WHERE thread_id = ? ORDER BY ts ASC, rowid ASC",
                (thread_id,),
            )
        else:
            rows = self._db.query(
                "SELECT msg_id, dir, text, ts, source FROM thread_messages "
                "WHERE thread_id = ? ORDER BY ts DESC, rowid DESC LIMIT ?",
                (thread_id, limit),
            )
            rows = list(reversed(rows))
        return [
            {
                "msg_id": r["msg_id"],
                "dir": r["dir"],
                "text": r["text"],
                "ts": r["ts"],
                "source": r["source"],
            }
            for r in rows
        ]

    _THREAD_WRITABLE = ("buyer_location", "agent_note", "listed_price", "listing_url")
    # The only status flips this generic writer owns. held is owned by hold/release, escalated by
    # escalate, and the sale states by the confirm-sold / buyer-accept flows.
    _THREAD_STATUS_TRANSITIONS = frozenset({("escalated", "active"), ("active", "closed")})

    def update_thread(self, thread_id: str, fields: dict) -> dict:
        status = fields.get("status")
        other = {k: v for k, v in fields.items() if k != "status"}
        unknown = [k for k in other if k not in self._THREAD_WRITABLE]
        if unknown:
            raise StoreError(
                f"unknown or non-writable thread field(s): {', '.join(sorted(unknown))}; "
                f"writable: {', '.join(self._THREAD_WRITABLE)} (transcript/cursor advance via "
                "send_reply/record_manual_reply; held via hold_thread; sale states via the "
                "confirm flows)"
            )
        if not fields:
            raise StoreError("no fields to update")
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if not row:
                raise ThreadNotFound(f"no thread with id {thread_id!r}")
            assignments = dict(other)
            if status is not None:
                if (row["status"], status) not in self._THREAD_STATUS_TRANSITIONS:
                    raise StoreError(
                        f"status {row['status']!r}->{status!r} is not allowed here; held is owned "
                        "by hold_thread/release_thread, escalated by escalate, and sale states by "
                        "negotiate_confirm_sold / buyer_negotiate_accept"
                    )
                assignments["status"] = status
                if status == "closed":
                    assignments["closed_ts"] = _now()
            if not assignments:
                raise StoreError("no fields to update")
            clause = ", ".join(f"{name} = ?" for name in assignments)
            conn.execute(
                f"UPDATE threads SET {clause}, updated_ts = ? WHERE thread_id = ?",
                (*assignments.values(), _now(), thread_id),
            )
        return self.get_thread(thread_id)  # type: ignore[return-value]

    def hold_thread(self, thread_id: str, reason: str, mark_handled_msg: str | None = None) -> dict:
        """Flip a thread to held, preserving the pre-hold status so release can restore it. A
        re-hold keeps the original held_from_status; mark_handled advances the cursor (the hold IS
        the handling — no reply is sent)."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT status, held_from_status FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if not row:
                raise ThreadNotFound(f"no thread with id {thread_id!r}")
            if row["status"] == "held":
                held_from = row["held_from_status"] or "active"
            else:
                held_from = row["status"] or "active"
            if mark_handled_msg:
                conn.execute(
                    "UPDATE threads SET status = 'held', held_reason = ?, held_from_status = ?, "
                    "cursor_last_msg_id = ?, cursor_last_ts = ?, updated_ts = ? "
                    "WHERE thread_id = ?",
                    (reason, held_from, mark_handled_msg, _now(), _now(), thread_id),
                )
            else:
                conn.execute(
                    "UPDATE threads SET status = 'held', held_reason = ?, held_from_status = ?, "
                    "updated_ts = ? WHERE thread_id = ?",
                    (reason, held_from, _now(), thread_id),
                )
        return self.get_thread(thread_id)  # type: ignore[return-value]

    def release_thread(self, thread_id: str) -> dict:
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT status, held_from_status FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if not row:
                raise ThreadNotFound(f"no thread with id {thread_id!r}")
            restored = row["held_from_status"] or "active"
            conn.execute(
                "UPDATE threads SET status = ?, held_reason = NULL, held_from_status = NULL, "
                "updated_ts = ? WHERE thread_id = ?",
                (restored, _now(), thread_id),
            )
        return self.get_thread(thread_id)  # type: ignore[return-value]

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
    ) -> dict:
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

    def get_want(self, want_id: str) -> dict | None:
        rows = self._db.query("SELECT * FROM wants WHERE want_id = ?", (want_id,))
        return _want_from_row(rows[0]) if rows else None

    def list_wants(self, status: str | None = None) -> list[dict]:
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

    def update_want(self, want_id: str, fields: dict) -> dict:
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
    ) -> dict:
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

    def get_budget(self, want_id: str) -> dict | None:
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

    # --- sell-side negotiation --------------------------------------------------------------

    def _load_negotiation(self, conn, item_id: str) -> dict:
        row = conn.execute("SELECT * FROM negotiations WHERE item_id = ?", (item_id,)).fetchone()
        if row:
            front_runner = json.loads(row["front_runner"]) if row["front_runner"] else None
            state, sold_to, is_bidding = row["state"], row["sold_to"], bool(row["is_bidding"])
        else:
            front_runner, state, sold_to, is_bidding = None, "open", None, False
        buyers = {}
        for b in conn.execute(
            "SELECT * FROM negotiation_buyers WHERE item_id = ?", (item_id,)
        ).fetchall():
            buyers[b["thread_id"]] = {
                "buyer_handle": b["buyer_handle"],
                "offers": json.loads(b["offers"]),
                "highest_offer": b["highest_offer"],
                "rounds_used": b["rounds_used"],
                "last_counter": b["last_counter"],
                "lowball_count": b["lowball_count"],
                "status": b["status"],
            }
        return {
            "state": state,
            "is_bidding": is_bidding,
            "front_runner": front_runner,
            "sold_to": sold_to,
            "buyers": buyers,
        }

    def _persist_negotiation(self, conn, item_id: str, led: dict) -> None:
        conn.execute(
            "INSERT INTO negotiations "
            "(item_id, state, is_bidding, front_runner, sold_to, updated_ts) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (item_id) DO UPDATE SET "
            "state = excluded.state, is_bidding = excluded.is_bidding, "
            "front_runner = excluded.front_runner, sold_to = excluded.sold_to, "
            "updated_ts = excluded.updated_ts",
            (
                item_id,
                led["state"],
                1 if led["is_bidding"] else 0,
                json.dumps(led["front_runner"], sort_keys=True) if led["front_runner"] else None,
                led["sold_to"],
                _now(),
            ),
        )
        for thread_id, b in led["buyers"].items():
            conn.execute(
                "INSERT INTO negotiation_buyers "
                "(item_id, thread_id, buyer_handle, offers, highest_offer, rounds_used, "
                " last_counter, lowball_count, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (item_id, thread_id) DO UPDATE SET "
                "buyer_handle = excluded.buyer_handle, offers = excluded.offers, "
                "highest_offer = excluded.highest_offer, rounds_used = excluded.rounds_used, "
                "last_counter = excluded.last_counter, lowball_count = excluded.lowball_count, "
                "status = excluded.status",
                (
                    item_id,
                    thread_id,
                    b["buyer_handle"],
                    json.dumps(b["offers"]),
                    b["highest_offer"],
                    b["rounds_used"],
                    b["last_counter"],
                    b["lowball_count"],
                    b["status"],
                ),
            )

    def _item_for_negotiation(self, conn, item_id: str) -> tuple:
        item = conn.execute(
            "SELECT list_price, currency FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if not item:
            raise ItemNotFound(f"no item with id {item_id!r}")
        return item["list_price"], item["currency"]

    def negotiate_offer(
        self, item_id: str, thread_id: str, handle: str, offer: float, *, config
    ) -> dict:
        """One offer, decided and persisted in a single transaction — the serialization that makes
        FCFS single-inventory hold. Floorless orchestration lives here (not the engine): at/above
        list persists the default floor, below list holds for the seller's floor ask, no list price
        is a data error; the engine is then handed a complete floor."""
        with self._db.transaction() as conn:
            list_price, currency = self._item_for_negotiation(conn, item_id)
            floor_row = conn.execute(
                "SELECT * FROM floors WHERE item_id = ?", (item_id,)
            ).fetchone()

            if floor_row is None:
                if not isinstance(list_price, (int, float)) or list_price <= 0:
                    raise StoreError(f"item {item_id!r} has no valid list price to negotiate")
                if offer < list_price:
                    return self._hold_for_floor(conn, item_id, thread_id, handle, offer, currency)
                # at/above list needs no real floor: persist the documented default (= list price)
                conn.execute(
                    "INSERT INTO floors (item_id, floor, currency, source, updated_ts) "
                    "VALUES (?, ?, ?, 'default', ?)",
                    (item_id, list_price, currency, _now()),
                )
                floor_row = conn.execute(
                    "SELECT * FROM floors WHERE item_id = ?", (item_id,)
                ).fetchone()

            floor = floor_row["floor"]
            floor_record = {
                "auto_counter_step": floor_row["auto_counter_step"],
                "auto_counter_rounds": floor_row["auto_counter_rounds"],
            }
            knobs = negotiate_engine.resolve_knobs(config, floor_record)

            led = self._load_negotiation(conn, item_id)
            buyer = led["buyers"].get(thread_id) or negotiate_engine.blank_buyer(handle)
            buyer["buyer_handle"] = handle
            negotiate_engine.record_offer(buyer, offer)

            res = negotiate_engine.decide(
                offer, thread_id, buyer, led, floor, list_price, knobs["step"], knobs
            )
            self._apply_offer_transition(led, thread_id, buyer, res)
            led["buyers"][thread_id] = buyer
            self._persist_negotiation(conn, item_id, led)
            res["item_state"] = led["state"]
            res["currency"] = currency
            return res

    def _hold_for_floor(self, conn, item_id, thread_id, handle, offer, currency) -> dict:
        """No floor and a below-list offer: record the offer and hold, so the caller asks the
        seller for the floor once. Nothing is decided (no rounds/front-runner consumed), and the
        held offer still bars rivals via other_best once the floor lands."""
        led = self._load_negotiation(conn, item_id)
        if led["state"] == "sold":
            return {
                "decision": "sold",
                "needs_seller_confirm": False,
                "message_intent": "item_sold",
                "item_state": "sold",
                "currency": currency,
            }
        buyer = led["buyers"].get(thread_id) or negotiate_engine.blank_buyer(handle)
        buyer["buyer_handle"] = handle
        negotiate_engine.record_offer(buyer, offer, held_for_floor=True)
        led["buyers"][thread_id] = buyer
        self._persist_negotiation(conn, item_id, led)
        return {
            "decision": "needs_floor",
            "counter_price": None,
            "hold_firm": False,
            "needs_seller_confirm": True,
            "message_intent": "hold_for_floor",
            "item_state": led["state"],
            "currency": currency,
        }

    @staticmethod
    def _apply_offer_transition(led, thread_id, buyer, res) -> None:
        decision = res["decision"]
        if decision == "counter":
            buyer["rounds_used"] += 1
            buyer["last_counter"] = res["counter_price"]
        elif decision == "deflect_lowball":
            buyer["lowball_count"] += 1
        elif decision == "hold_firm" and res["message_intent"] == "disengage":
            buyer["status"] = "passed"
        elif decision == "accept_fcfs":
            buyer["status"] = "front_runner"
            led["front_runner"] = {
                "thread_id": thread_id,
                "amount": res["accept_price"],
                "kind": "fcfs",
            }
            led["state"] = "reserved_provisional"
        elif decision == "bid_lead":
            buyer["status"] = "leading_bid"
            led["is_bidding"] = True
            led["state"] = "bidding"
            led["front_runner"] = {
                "thread_id": thread_id,
                "amount": res["leading_amount"],
                "kind": "bid",
            }
        elif decision == "bid_outbid":
            led["is_bidding"] = True
            led["state"] = "bidding"

    def negotiate_status(self, item_id: str) -> dict:
        with self._db.transaction() as conn:
            self._item_for_negotiation(conn, item_id)
            led = self._load_negotiation(conn, item_id)
        return {
            "item_state": led["state"],
            "is_bidding": led["is_bidding"],
            "front_runner": led["front_runner"],
            "buyers": {
                t: {"status": b["status"], "highest_offer": b["highest_offer"]}
                for t, b in led["buyers"].items()
            },
        }

    def negotiate_confirm_bid(self, item_id: str, thread_id: str) -> dict:
        with self._db.transaction() as conn:
            self._item_for_negotiation(conn, item_id)
            led = self._load_negotiation(conn, item_id)
            fr = led["front_runner"]
            if not fr or fr.get("thread_id") != thread_id:
                raise StoreError("thread is not the current leading bid")
            led["state"] = "reserved_provisional"
            led["buyers"][thread_id]["status"] = "won"
            for tid, b in led["buyers"].items():
                if tid != thread_id and b.get("status") != "passed":
                    b["status"] = "outbid"
            self._persist_negotiation(conn, item_id, led)
            return {
                "reserved_for": thread_id,
                "amount": fr["amount"],
                "item_state": led["state"],
                "tell_others": "outbid",
            }

    def negotiate_confirm_sold(self, item_id: str, thread_id: str) -> dict:
        with self._db.transaction() as conn:
            row = conn.execute("SELECT listing_urls FROM items WHERE id = ?", (item_id,)).fetchone()
            if not row:
                raise ItemNotFound(f"no item with id {item_id!r}")
            led = self._load_negotiation(conn, item_id)
            led["state"] = "sold"
            led["sold_to"] = thread_id
            urls = json.loads(row["listing_urls"])
            won_platform = thread_id.split(":", 1)[0] if ":" in thread_id else None
            take_down = [
                {"platform": p, "url": u} for p, u in urls.items() if u and p != won_platform
            ]
            close = [
                t
                for t, b in led["buyers"].items()
                if t != thread_id and b.get("status") not in ("lost", "passed")
            ]
            for t in close:
                led["buyers"][t]["status"] = "lost"
            self._persist_negotiation(conn, item_id, led)
            return {"item_state": "sold", "take_down": take_down, "close_threads": close}

    def negotiate_release(self, item_id: str) -> dict:
        with self._db.transaction() as conn:
            self._item_for_negotiation(conn, item_id)
            led = self._load_negotiation(conn, item_id)
            led["state"] = "bidding" if led["is_bidding"] else "open"
            led["front_runner"] = None
            for b in led["buyers"].values():
                if b.get("status") in ("front_runner", "won"):
                    b["status"] = "active"
            self._persist_negotiation(conn, item_id, led)
            return {"item_state": led["state"]}

    # --- buy-side negotiation ---------------------------------------------------------------

    def _budget_for_engine(self, want_id: str) -> dict:
        rec = self.get_budget(want_id)
        if rec is None:
            raise StoreError(f"no budget set for want {want_id!r} — set one before negotiating")
        return {
            "target": rec["target_price"],
            "max_budget": rec["max_budget"],
            "currency": rec["currency"] or "",
            "step": rec["auto_counter_step"] or buyer_engine.DEFAULT_STEP,
            "max_rounds": rec["auto_counter_rounds"] or buyer_engine.DEFAULT_MAX_ROUNDS,
            "opening_ratio": rec["opening_ratio"] or buyer_engine.DEFAULT_OPENING_RATIO,
        }

    def _load_buyer_negotiation(self, conn, want_id: str) -> dict:
        row = conn.execute(
            "SELECT * FROM buyer_negotiations WHERE want_id = ?", (want_id,)
        ).fetchone()
        state = row["state"] if row else "shopping"
        committed_thread = row["committed_thread"] if row else None
        sellers = {}
        for s in conn.execute(
            "SELECT * FROM buyer_negotiation_sellers WHERE want_id = ?", (want_id,)
        ).fetchall():
            sellers[s["thread_id"]] = {
                "seller_handle": s["seller_handle"],
                "listed_price": s["listed_price"],
                "our_offers": json.loads(s["our_offers"]),
                "our_highest_offer": s["our_highest_offer"],
                "seller_lowest_ask": s["seller_lowest_ask"],
                "rounds_used": s["rounds_used"],
                "last_offer": s["last_offer"],
                "agreed_price": s["agreed_price"],
                "status": s["status"],
            }
        return {"state": state, "committed_thread": committed_thread, "sellers": sellers}

    def _persist_buyer_negotiation(self, conn, want_id: str, led: dict) -> None:
        conn.execute(
            "INSERT INTO buyer_negotiations (want_id, state, committed_thread, updated_ts) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (want_id) DO UPDATE SET "
            "state = excluded.state, committed_thread = excluded.committed_thread, "
            "updated_ts = excluded.updated_ts",
            (want_id, led["state"], led["committed_thread"], _now()),
        )
        for thread_id, s in led["sellers"].items():
            conn.execute(
                "INSERT INTO buyer_negotiation_sellers "
                "(want_id, thread_id, seller_handle, listed_price, our_offers, our_highest_offer, "
                " seller_lowest_ask, rounds_used, last_offer, agreed_price, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (want_id, thread_id) DO UPDATE SET "
                "seller_handle = excluded.seller_handle, listed_price = excluded.listed_price, "
                "our_offers = excluded.our_offers, "
                "our_highest_offer = excluded.our_highest_offer, "
                "seller_lowest_ask = excluded.seller_lowest_ask, "
                "rounds_used = excluded.rounds_used, "
                "last_offer = excluded.last_offer, agreed_price = excluded.agreed_price, "
                "status = excluded.status",
                (
                    want_id,
                    thread_id,
                    s["seller_handle"],
                    s["listed_price"],
                    json.dumps(s["our_offers"]),
                    s["our_highest_offer"],
                    s["seller_lowest_ask"],
                    s["rounds_used"],
                    s["last_offer"],
                    s["agreed_price"],
                    s["status"],
                ),
            )

    def _require_want(self, conn, want_id: str) -> None:
        if not conn.execute("SELECT 1 FROM wants WHERE want_id = ?", (want_id,)).fetchone():
            raise WantNotFound(f"no want with id {want_id!r}")

    def buyer_negotiate_seed(
        self,
        want_id: str,
        thread_id: str,
        seller_handle: str,
        *,
        listed_price=None,
        our_last=None,
        seller_ask=None,
        rounds=None,
    ) -> dict:
        """Seed a seller entry for a thread the user started by hand, without emitting an offer or
        reading the budget — records the user's prior offer so the next reply climbs from it."""
        with self._db.transaction() as conn:
            self._require_want(conn, want_id)
            led = self._load_buyer_negotiation(conn, want_id)
            seller = led["sellers"].get(thread_id) or buyer_engine.blank_seller(
                seller_handle, listed_price
            )
            if seller_handle:
                seller["seller_handle"] = seller_handle
            if listed_price is not None:
                seller["listed_price"] = listed_price
            if seller_ask is not None:
                seller["seller_lowest_ask"] = (
                    seller_ask
                    if seller["seller_lowest_ask"] is None
                    else min(seller["seller_lowest_ask"], seller_ask)
                )
            if our_last is not None:
                amt = int(our_last)
                seller["our_offers"].append({"amount": amt, "source": "user_prior"})
                seller["our_highest_offer"] = max(seller["our_highest_offer"], amt)
                seller["last_offer"] = amt
            if rounds is not None:
                seller["rounds_used"] = max(seller["rounds_used"], int(rounds))
            seller["status"] = "negotiating"
            led["sellers"][thread_id] = seller
            self._persist_buyer_negotiation(conn, want_id, led)
            return {
                "decision": "seeded",
                "thread": thread_id,
                "our_last": seller["last_offer"],
                "seller_lowest_ask": seller["seller_lowest_ask"],
                "rounds_used": seller["rounds_used"],
                "want_state": led["state"],
            }

    def buyer_negotiate_open(
        self, want_id: str, thread_id: str, seller_handle: str, listed: float, ask=None
    ) -> dict:
        budget = self._budget_for_engine(want_id)
        with self._db.transaction() as conn:
            self._require_want(conn, want_id)
            led = self._load_buyer_negotiation(conn, want_id)
            seller = led["sellers"].get(thread_id) or buyer_engine.blank_seller(
                seller_handle, listed
            )
            seller["seller_handle"] = seller_handle
            seller["listed_price"] = listed
            if ask is not None:
                seller["seller_lowest_ask"] = (
                    ask
                    if seller["seller_lowest_ask"] is None
                    else min(seller["seller_lowest_ask"], ask)
                )
            res = buyer_engine.guard(
                buyer_engine.decide_open(
                    listed, budget["target"], budget["max_budget"], budget["opening_ratio"]
                ),
                budget["max_budget"],
            )
            if res["decision"] == "opening_offer":
                amt = res["offer_price"]
                seller["our_offers"].append({"amount": amt})
                seller["our_highest_offer"] = max(seller["our_highest_offer"], amt)
                seller["last_offer"] = amt
            elif res["decision"] == "accept":
                seller["status"] = "deal_pending"
                seller["agreed_price"] = res["accept_price"]
            led["sellers"][thread_id] = seller
            self._persist_buyer_negotiation(conn, want_id, led)
            res["want_state"] = led["state"]
            res["currency"] = budget["currency"]
            return res

    def buyer_negotiate_reply(self, want_id: str, thread_id: str, seller_price: float) -> dict:
        budget = self._budget_for_engine(want_id)
        with self._db.transaction() as conn:
            self._require_want(conn, want_id)
            led = self._load_buyer_negotiation(conn, want_id)
            seller = led["sellers"].get(thread_id) or buyer_engine.blank_seller("", seller_price)
            seller["seller_lowest_ask"] = (
                seller_price
                if seller["seller_lowest_ask"] is None
                else min(seller["seller_lowest_ask"], seller_price)
            )
            res = buyer_engine.guard(
                buyer_engine.decide_reply(
                    seller_price,
                    seller,
                    led,
                    thread_id,
                    budget["target"],
                    budget["max_budget"],
                    budget["step"],
                    budget["max_rounds"],
                ),
                budget["max_budget"],
            )
            decision = res["decision"]
            if decision == "counter":
                amt = res["offer_price"]
                seller["our_offers"].append({"amount": amt})
                seller["our_highest_offer"] = max(seller["our_highest_offer"], amt)
                seller["last_offer"] = amt
                seller["rounds_used"] += 1
            elif decision == "accept":
                seller["status"] = "deal_pending"
                seller["agreed_price"] = res["accept_price"]
            elif decision == "walk_away":
                seller["status"] = "walked"
            led["sellers"][thread_id] = seller
            self._persist_buyer_negotiation(conn, want_id, led)
            res["want_state"] = led["state"]
            res["currency"] = budget["currency"]
            return res

    def buyer_negotiate_accept(self, want_id: str, thread_id: str) -> dict:
        budget = self._budget_for_engine(want_id)
        with self._db.transaction() as conn:
            self._require_want(conn, want_id)
            led = self._load_buyer_negotiation(conn, want_id)
            seller = led["sellers"].get(thread_id)
            if not seller:
                raise StoreError("no such thread on this want")
            led["state"] = "committed"
            led["committed_thread"] = thread_id
            seller["status"] = "committed"
            close = [
                t
                for t, s in led["sellers"].items()
                if t != thread_id and s.get("status") not in ("walked", "lost", "unavailable")
            ]
            for t in close:
                led["sellers"][t]["status"] = "lost"
            self._persist_buyer_negotiation(conn, want_id, led)
            return {
                "committed_thread": thread_id,
                "deal_price": seller.get("agreed_price") or seller.get("last_offer"),
                "close_threads": close,
                "want_state": "committed",
                "currency": budget["currency"],
            }

    def buyer_negotiate_walk(self, want_id: str, thread_id: str) -> dict:
        with self._db.transaction() as conn:
            self._require_want(conn, want_id)
            led = self._load_buyer_negotiation(conn, want_id)
            seller = led["sellers"].get(thread_id)
            if not seller:
                raise StoreError("no such thread on this want")
            seller["status"] = "walked"
            self._persist_buyer_negotiation(conn, want_id, led)
            return {"thread": thread_id, "want_state": led["state"]}

    # --- seller config ----------------------------------------------------------------------

    # The origin section holds the exact street address — stored, never returned by a read tool.
    _SELLER_CONFIG_PRIVATE = frozenset({"origin"})

    def set_seller_config_section(self, section: str, value: dict) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO seller_config (section, value, updated_ts) VALUES (?, ?, ?) "
                "ON CONFLICT (section) DO UPDATE SET value = excluded.value, "
                "updated_ts = excluded.updated_ts",
                (section, json.dumps(value, sort_keys=True), _now()),
            )

    def get_seller_config_section(self, section: str) -> dict | None:
        """Internal read of one section. quote_shipping uses it for basics/shipping; nothing reads
        the origin section back — it is write-only from the tool layer."""
        rows = self._db.query("SELECT value FROM seller_config WHERE section = ?", (section,))
        return json.loads(rows[0]["value"]) if rows else None

    def get_seller_config_public(self) -> dict:
        """Every section except the private origin address — the buyer-safe view a read tool may
        return."""
        rows = self._db.query("SELECT section, value FROM seller_config")
        return {
            r["section"]: json.loads(r["value"])
            for r in rows
            if r["section"] not in self._SELLER_CONFIG_PRIVATE
        }

    # --- escalations ------------------------------------------------------------------------

    def escalate(
        self,
        thread_id: str,
        *,
        open_question: str,
        kind: str | None = None,
        context_summary: str | None = None,
    ) -> dict:
        """Open an escalation against a REAL thread (no synthetic ids — the 2026-06-29 incident):
        it records the open question, flips the thread to escalated, and is idempotent — a second
        escalate on a thread with an open escalation returns the existing id, changing nothing."""
        with self._db.transaction() as conn:
            thread = conn.execute(
                "SELECT side, item_id, want_id FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if not thread:
                raise ThreadNotFound(f"no thread with id {thread_id!r}")
            existing = conn.execute(
                "SELECT id FROM escalations WHERE thread_id = ? AND status = 'open'", (thread_id,)
            ).fetchone()
            if existing:
                return {"id": existing["id"], "idempotent": True}
            esc_id = _new_id("esc")
            conn.execute(
                "INSERT INTO escalations "
                "(id, thread_id, side, item_id, want_id, kind, open_question, context_summary, "
                " status, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (
                    esc_id,
                    thread_id,
                    thread["side"],
                    thread["item_id"],
                    thread["want_id"],
                    kind,
                    open_question,
                    context_summary,
                    _now(),
                ),
            )
            conn.execute(
                "UPDATE threads SET status = 'escalated', updated_ts = ? WHERE thread_id = ?",
                (_now(), thread_id),
            )
            return {"id": esc_id, "idempotent": False}

    def resolve_escalation(self, escalation_id: str, resolution: str) -> dict:
        """Stamp an escalation resolved. Thread reactivation stays the caller's update_thread —
        this only closes the escalation record (the substrate any alarm path checks first)."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM escalations WHERE id = ?", (escalation_id,)
            ).fetchone()
            if not row:
                raise StoreError(f"no escalation with id {escalation_id!r}")
            conn.execute(
                "UPDATE escalations SET status = 'resolved', resolution = ?, resolved_ts = ? "
                "WHERE id = ?",
                (resolution, _now(), escalation_id),
            )
        return {"id": escalation_id, "status": "resolved"}

    def count_open_escalations(self) -> int:
        rows = self._db.query("SELECT COUNT(*) AS n FROM escalations WHERE status = 'open'")
        return rows[0]["n"]

    def open_escalation_thread_ids(self) -> set:
        """Threads with an open escalation — excluded from follow-up eligibility."""
        rows = self._db.query("SELECT DISTINCT thread_id FROM escalations WHERE status = 'open'")
        return {r["thread_id"] for r in rows}

    # --- pacing -----------------------------------------------------------------------------

    def reserve_action(
        self,
        *,
        marketplace: str,
        kind: str,
        cfg,
        now: float | None = None,
        interactive: bool = False,
    ) -> dict:
        """Atomic check-and-record: count this marketplace's in-window actions, decide, and — only
        on `go` — insert the action and compact the ledger, all in one transaction (the
        serialization the legacy flock gave). On `go` the caller sleeps the returned jitter AFTER
        this returns; the DB lock is never held across that sleep."""
        now = now if now is not None else _now()
        cutoff = now - pacing_engine.WINDOW_SECONDS
        with self._db.transaction() as conn:
            rows = conn.execute(
                "SELECT ts FROM pacing_actions WHERE marketplace = ? AND ts > ?",
                (marketplace, cutoff),
            ).fetchall()
            timestamps = [r["ts"] for r in rows]
            result = pacing_engine.evaluate(
                timestamps, now=now, cfg=cfg, kind=kind, interactive=interactive
            )
            if result["record"]:
                conn.execute(
                    "INSERT INTO pacing_actions (marketplace, kind, ts) VALUES (?, ?, ?)",
                    (marketplace, kind, now),
                )
                conn.execute(
                    "DELETE FROM pacing_actions WHERE marketplace = ? AND ts <= ?",
                    (marketplace, cutoff),
                )
        result["marketplace"] = marketplace
        return result

    # --- scam signatures --------------------------------------------------------------------

    _SCAM_LEGAL_FROM = {
        "confirmed": {"observed", "confirmed"},
        "dismissed": {"observed", "confirmed"},
        "shared": {"confirmed", "shared"},
    }

    def add_scam_signature(
        self,
        *,
        kind: str,
        value: str,
        marketplace: str,
        thread_id: str = "",
        context: str = "",
        play: str | None = None,
        severity: str = "medium",
        detected_by: str = "detect",
    ) -> dict:
        """Append a signature to the local bank, deduped by its deterministic id. Registry-sourced
        and seller-confirmed rows are born confirmed; a detector sighting is born observed."""
        norm = scam_engine.normalize(kind, value)
        if not norm:
            raise StoreError("empty scam signature value")
        sig_id = scam_engine.make_id(kind, value)
        confirmed = scam_engine.born_confirmed(detected_by)
        ts = _now()
        with self._db.transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM scam_signatures WHERE id = ?", (sig_id,)
            ).fetchone()
            if existing:
                return {"id": sig_id, "deduped": True}
            conn.execute(
                "INSERT INTO scam_signatures "
                "(id, kind, value, play, marketplace, thread_id, context, detected_by, severity, "
                " status, added_ts, confirmed_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sig_id,
                    kind,
                    norm,
                    play,
                    marketplace,
                    thread_id,
                    context,
                    detected_by,
                    severity,
                    "confirmed" if confirmed else "observed",
                    ts,
                    ts if confirmed else None,
                ),
            )
        return {"id": sig_id, "deduped": False}

    def _scam_bank_rows(self) -> list[dict]:
        rows = self._db.query("SELECT * FROM scam_signatures")
        return [
            {
                "id": r["id"],
                "kind": r["kind"],
                "value": r["value"],
                "play": r["play"],
                "severity": r["severity"],
                "status": r["status"],
                "detected_by": r["detected_by"],
                "thread_id": r["thread_id"],
            }
            for r in rows
        ]

    def merged_scam_signatures(self) -> tuple:
        """The deterministic match set the scan consumes plus a registry_ok flag: the packaged
        registry ∪ the active local bank (registry wins ties, dismissed suppresses both tiers).
        An unreadable/malformed registry degrades to bank-only (registry_ok False)."""
        registry, registry_ok = _load_scam_registry()
        merged = scam_engine.merge_signatures(registry, self._scam_bank_rows())
        return merged, registry_ok

    def transition_scam_signature(self, sig_id: str, to_status: str) -> dict:
        legal_from = self._SCAM_LEGAL_FROM.get(to_status)
        if legal_from is None:
            raise StoreError(f"unknown scam status transition {to_status!r}")
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM scam_signatures WHERE id = ?", (sig_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"no scam signature {sig_id!r}")
            current = row["status"]
            if current == to_status:
                return {"id": sig_id, "status": to_status, "noop": True}
            if current not in legal_from:
                raise StoreError(f"cannot {to_status} a {current!r} signature")
            stamp = (
                "confirmed_ts"
                if to_status == "confirmed"
                else ("shared_ts" if to_status == "shared" else None)
            )
            if stamp:
                conn.execute(
                    f"UPDATE scam_signatures SET status = ?, {stamp} = ? WHERE id = ?",
                    (to_status, _now(), sig_id),
                )
            else:
                conn.execute(
                    "UPDATE scam_signatures SET status = ? WHERE id = ?", (to_status, sig_id)
                )
        return {"id": sig_id, "status": to_status}

    def retract_detect_scam(self, thread_id: str) -> int:
        """Drop detect-sourced bank rows for a thread (a false-positive undo). Seller-confirmed and
        registry-born rows are never auto-dropped. Returns the number removed."""
        with self._db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM scam_signatures WHERE detected_by = 'detect' AND thread_id = ?",
                (thread_id,),
            )
            return cur.rowcount

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

    def get_pass(self, pass_id: str) -> dict | None:
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


@dataclass(frozen=True)
class Scope:
    """The entity scope a headless pass was spawned with: the threads it may touch plus their
    owning items and wants. Attended sessions run unscoped (Session.scope is None)."""

    thread_ids: frozenset = frozenset()
    item_ids: frozenset = frozenset()
    want_ids: frozenset = frozenset()

    @classmethod
    def of(cls, *, threads=(), items=(), wants=()) -> Scope:
        return cls(frozenset(threads), frozenset(items), frozenset(wants))

    def allows(self, kind: str, value) -> bool:
        # An absent optional id (None) is not an out-of-scope reference — it is simply not set.
        if value is None:
            return True
        return (
            value
            in {
                "thread": self.thread_ids,
                "item": self.item_ids,
                "want": self.want_ids,
            }[kind]
        )

    def to_json(self) -> dict:
        return {
            "thread_ids": sorted(self.thread_ids),
            "item_ids": sorted(self.item_ids),
            "want_ids": sorted(self.want_ids),
        }

    @classmethod
    def from_json(cls, data: dict) -> Scope:
        return cls.of(
            threads=data.get("thread_ids", ()),
            items=data.get("item_ids", ()),
            wants=data.get("want_ids", ()),
        )


# Accessors that take a scoped id. For a scoped session every listed id argument must be in
# scope, or the call answers exactly as it would for a row that does not exist — an out-of-scope
# id must be indistinguishable from an absent one, so scope never leaks existence. Each entry is
# (name -> ((param, kind), ...)); later plans extend it as engine/mutation accessors land.
_SCOPE_GUARDED = {
    "get_item": (("item_id", "item"),),
    "get_thread": (("thread_id", "thread"),),
    "get_thread_messages": (("thread_id", "thread"),),
    "append_thread_message": (("thread_id", "thread"),),
    "update_thread": (("thread_id", "thread"),),
    "hold_thread": (("thread_id", "thread"),),
    "release_thread": (("thread_id", "thread"),),
    "escalate": (("thread_id", "thread"),),
    "get_want": (("want_id", "want"),),
    "update_want": (("want_id", "want"),),
    "cancel_want": (("want_id", "want"),),
    "negotiate_offer": (("item_id", "item"), ("thread_id", "thread")),
    "negotiate_status": (("item_id", "item"),),
    "negotiate_confirm_bid": (("item_id", "item"), ("thread_id", "thread")),
    "negotiate_confirm_sold": (("item_id", "item"), ("thread_id", "thread")),
    "negotiate_release": (("item_id", "item"),),
    "set_budget": (("want_id", "want"),),
    "buyer_negotiate_seed": (("want_id", "want"), ("thread_id", "thread")),
    "buyer_negotiate_open": (("want_id", "want"), ("thread_id", "thread")),
    "buyer_negotiate_reply": (("want_id", "want"), ("thread_id", "thread")),
    "buyer_negotiate_accept": (("want_id", "want"), ("thread_id", "thread")),
    "buyer_negotiate_walk": (("want_id", "want"), ("thread_id", "thread")),
}

# What a guarded accessor does when an id is out of scope: mirror the accessor's own
# missing-row behavior so the two are indistinguishable. Reads that return None on a missing
# row return None; the transcript read returns []; row-required writers raise the same NotFound.
_SCOPE_MISS_NONE = frozenset({"get_item", "get_thread", "get_want"})
_SCOPE_MISS_EMPTY = frozenset({"get_thread_messages"})
_SCOPE_MISS_NOTFOUND = {
    "append_thread_message": ("thread", ThreadNotFound),
    "update_thread": ("thread", ThreadNotFound),
    "hold_thread": ("thread", ThreadNotFound),
    "release_thread": ("thread", ThreadNotFound),
    "escalate": ("thread", ThreadNotFound),
    "update_want": ("want", WantNotFound),
    "cancel_want": ("want", WantNotFound),
    "negotiate_offer": ("item", ItemNotFound),
    "negotiate_status": ("item", ItemNotFound),
    "negotiate_confirm_bid": ("item", ItemNotFound),
    "negotiate_confirm_sold": ("item", ItemNotFound),
    "negotiate_release": ("item", ItemNotFound),
    "set_budget": ("want", WantNotFound),
    "buyer_negotiate_seed": ("want", WantNotFound),
    "buyer_negotiate_open": ("want", WantNotFound),
    "buyer_negotiate_reply": ("want", WantNotFound),
    "buyer_negotiate_accept": ("want", WantNotFound),
    "buyer_negotiate_walk": ("want", WantNotFound),
}


class ScopedStore:
    """A scope-aware view over a Store. Unscoped (scope=None) it is a transparent pass-through —
    attended sessions hold full scope. Scoped, it enforces the entity scope at every guarded
    accessor, answering an out-of-scope id exactly as a missing row. List accessors are filtered
    to the scope rather than rejected."""

    def __init__(self, store: Store, scope: Scope | None = None):
        self._store = store
        self._scope = scope

    # List reads are filtered to the scope (a scoped pass enumerates only its own entities).
    def list_threads(self, side: str | None = None, status: str | None = None) -> list[dict]:
        rows = self._store.list_threads(side=side, status=status)
        if self._scope is None:
            return rows
        return [r for r in rows if r["thread_id"] in self._scope.thread_ids]

    def list_items(self, status: str | None = None) -> list[dict]:
        rows = self._store.list_items(status=status)
        if self._scope is None:
            return rows
        return [r for r in rows if r["id"] in self._scope.item_ids]

    def list_wants(self, status: str | None = None) -> list[dict]:
        rows = self._store.list_wants(status=status)
        if self._scope is None:
            return rows
        return [r for r in rows if r["want_id"] in self._scope.want_ids]

    def __getattr__(self, name: str):
        target = getattr(self._store, name)
        spec = _SCOPE_GUARDED.get(name)
        if self._scope is None or spec is None:
            return target
        sig = inspect.signature(target)

        def guarded(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            for param, kind in spec:
                if not self._scope.allows(kind, bound.arguments.get(param)):
                    return self._deny(name, bound.arguments)
            return target(*args, **kwargs)

        return guarded

    def _deny(self, name: str, arguments: dict):
        if name in _SCOPE_MISS_NONE:
            return None
        if name in _SCOPE_MISS_EMPTY:
            return []
        kind, exc = _SCOPE_MISS_NOTFOUND[name]
        param = _SCOPE_GUARDED[name][0][0]
        raise exc(f"no {kind} with id {arguments.get(param)!r}")
