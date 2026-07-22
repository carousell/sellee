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

    # --- budgets ----------------------------------------------------------------------------

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
    "get_want": (("want_id", "want"),),
}

# What a guarded accessor does when an id is out of scope: mirror the accessor's own
# missing-row behavior so the two are indistinguishable. Reads that return None on a missing
# row return None; the transcript read returns []; row-required writers raise the same NotFound.
_SCOPE_MISS_NONE = frozenset({"get_item", "get_thread", "get_want"})
_SCOPE_MISS_EMPTY = frozenset({"get_thread_messages"})
_SCOPE_MISS_NOTFOUND = {"append_thread_message": ("thread", ThreadNotFound)}


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
