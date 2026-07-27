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

import hashlib
import inspect
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from selly_agent import marketplaces, paths
from selly_agent.db import Database
from selly_agent.engines import buyer_negotiate as buyer_engine
from selly_agent.engines import negotiate as negotiate_engine
from selly_agent.engines import pacing as pacing_engine
from selly_agent.engines import scam as scam_engine

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
    "photos",
)
_ITEM_STATUSES = ("draft", "ready")
# Photos are capped per item — the marketplace shows a handful, and an unbounded list would make
# the upload bracket (mint URL, POST, repeat) run for minutes.
MAX_PHOTOS = 12

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


# --- record & ack shapes ---------------------------------------------------------------------
#
# The store's stable returns are TypedDicts: a plain dict at runtime (bodies, call sites, and
# JSON serialization are all unchanged), but a declared shape the type checker enforces. Every
# key is always present — a nullable column surfaces as a `… | None` *value*, never an absent
# key — so the whole record being absent is the only optionality, carried by the
# `-> Record | None` return type. Discriminated-union returns (negotiate_*, the send bracket,
# the gate checks) stay a bare `dict`: which keys are present depends on the decision, which a
# single TypedDict cannot express.
#
# Two shapes encode the money-path secret discipline structurally: FloorAck and BudgetAck carry
# provenance only and have no value field, so a checker — not just a test — proves an ack cannot
# carry the floor/budget out. The value-bearing FloorRecord/BudgetRecord are returned only by the
# engine-facing get_floor/get_budget, which no LLM-facing tool reaches.


class ItemRecord(TypedDict):
    """The buyer-safe item view (see `_item_from_row`) — never a floor."""

    id: str
    title: str
    description: str
    condition: str | None
    list_price: float | None
    currency: str | None
    status: str
    size_bucket: str | None
    listing_urls: dict[str, str]
    photos: list
    created_ts: float
    updated_ts: float


class FloorAck(TypedDict):
    """The provenance-only receipt `set_floor` returns — structurally no floor value."""

    status: str
    item_id: str
    source: str
    replaced: str | None


class FloorRecord(TypedDict):
    """The confidential floor record, value-bearing — returned only by the engine-facing
    get_floor, which no LLM-facing tool calls."""

    item_id: str
    floor: float
    currency: str | None
    source: str
    auto_counter_step: int | None
    auto_counter_rounds: int | None
    updated_ts: float


class MessageRecord(TypedDict):
    """One transcript row (see `get_thread_messages`)."""

    msg_id: str
    dir: str
    text: str
    ts: float
    source: str | None


class ThreadSummary(TypedDict):
    """A thread's identity/status/cursor fields (see `_thread_from_row`) — what list_threads
    returns, with no transcript. Never a floor/budget."""

    thread_id: str
    side: str
    market: str
    item_id: str | None
    want_id: str | None
    counterpart_handle: str
    status: str
    held_reason: str | None
    held_from_status: str | None
    buyer_location: str | None
    agent_note: str | None
    listing_url: str | None
    listed_price: float | None
    close_method: str | None
    closed_ts: float | None
    closed_reason: str | None
    cursor_last_msg_id: str | None
    cursor_last_ts: float | None
    last_followup_ts: float | None
    followup_disposition: str | None
    source: str | None
    created_ts: float
    updated_ts: float


class ThreadRecord(ThreadSummary):
    """A single thread with its (capped) transcript folded in — what get_thread returns."""

    messages: list[MessageRecord]
    message_count: int


class WantRecord(TypedDict):
    """The buyer-safe want view (see `_want_from_row`) — never the max budget."""

    want_id: str
    query: str
    category: str | None
    condition_pref: str | None
    region: str | None
    currency: str | None
    target_price: float | None
    status: str
    source: str | None
    cancelled_ts: float | None
    cancel_reason: str | None
    created_ts: float
    updated_ts: float
    candidates: list
    shortlist: list


class BudgetAck(TypedDict):
    """The provenance-only receipt `set_budget` returns — structurally no budget value."""

    status: str
    want_id: str
    source: str
    replaced: str | None


class BudgetRecord(TypedDict):
    """The confidential budget record, value-bearing — returned only by the engine-facing
    get_budget, which no LLM-facing tool calls."""

    want_id: str
    max_budget: float
    target_price: float | None
    currency: str | None
    opening_ratio: float | None
    auto_counter_step: int | None
    auto_counter_rounds: int | None
    give_up_polls: int | None
    source: str
    updated_ts: float


class CheckoutRecord(TypedDict):
    """A persisted checkout link (see `get_checkout`)."""

    sale_id: str
    item_id: str
    thread_id: str | None
    checkout_url: str
    price: float | None
    currency: str | None
    issued_ts: float


# `class` is a reserved word, so PassRecord needs the functional syntax. Its annotations are
# strings (forward refs) so the `X | None` unions are never evaluated at runtime on the 3.9 floor;
# the checker still resolves them.
PassRecord = TypedDict(
    "PassRecord",
    {
        "pass_id": str,
        "type": str,
        "payload": dict,
        "status": str,
        "rc": "int | None",
        "class": "str | None",
        "summary": "str | None",
        "requested_ts": float,
        "started_ts": "float | None",
        "finished_ts": "float | None",
    },
)


class ChannelRecord(TypedDict):
    """The bound-channel singleton (see `get_channel`) — synthesized to defaults when no row
    exists yet (unbound: chat_id/bind_nonce None, update_offset 0). Holds no secret (the bot
    token lives in its own 0600 file, never here)."""

    adapter: str
    bot_username: str | None
    chat_id: int | None
    update_offset: int
    bind_nonce: str | None
    welcomed_at: float | None
    commands_hash: str | None
    bound_ts: float | None
    updated_ts: float | None


class InboxRecord(TypedDict):
    """One durable inbox row (see `_inbox_from_row`). payload/media_paths are decoded from JSON;
    src_ts is Telegram's own clock (informational), received_ts the local journal clock."""

    id: int
    event_id: int
    kind: str
    text: str | None
    payload: dict
    media_paths: list
    src_ts: float | None
    received_ts: float
    status: str
    handled_by: str | None
    pass_id: str | None
    updated_ts: float


class NoticeRecord(TypedDict):
    """One needs-me notice (see `_notice_from_row`) — a queued or delivered outbound message.
    `holdable` marks a proactive notice the drain may defer during quiet hours (seller-facing
    notices are not holdable and deliver at any hour); `controls` is an optional provider-neutral
    list of [label, token] button pairs the channel renders into a native keyboard."""

    id: int
    text: str
    ref: str | None
    created_ts: float
    status: str
    attempts: int
    delivered_ts: float | None
    via: str | None
    pass_id: str | None
    holdable: bool
    controls: list | None


class EscalationRecord(TypedDict):
    """One escalation row (see `_escalation_from_row`). Every key present; a null column is a
    `… | None` value, so the whole record being absent is the only optionality."""

    id: str
    thread_id: str
    side: str | None
    item_id: str | None
    want_id: str | None
    kind: str | None
    open_question: str
    context_summary: str | None
    status: str
    resolution: str | None
    created_ts: float
    resolved_ts: float | None


class TranscriptEntry(TypedDict):
    """One interleaved conversational-memory entry (see `recent_transcript`): an inbound inbox row
    or the agent's own outbound notice, keyed by the local clock so both sides order together."""

    direction: str
    kind: str
    text: str
    ts: float


def _item_from_row(row: sqlite3.Row) -> ItemRecord:
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
        "photos": json.loads(row["photos"]),
        "created_ts": row["created_ts"],
        "updated_ts": row["updated_ts"],
    }


def validate_photos(value: object) -> list:
    """Canonicalize an item's photo list, refusing anything outside the media store.

    A photo entry is {path, uploaded_url?}; a bare string path is accepted as shorthand. The gate
    is a *containment* check on the fully resolved path — a `..` segment or a symlink pointing
    somewhere else resolves out of the media store and is refused, so a stored path can never
    address a file the agent was not handed.
    """
    if not isinstance(value, (list, tuple)):
        raise StoreError("photos must be a list of {path, uploaded_url?} entries")
    if len(value) > MAX_PHOTOS:
        raise StoreError(f"at most {MAX_PHOTOS} photos per item")
    root = paths.media_dir().resolve()
    out: list = []
    for entry in value:
        if isinstance(entry, str):
            entry = {"path": entry}
        if not isinstance(entry, dict):
            raise StoreError("each photo must be a path string or a {path, uploaded_url?} object")
        unknown = sorted(set(entry) - {"path", "uploaded_url"})
        if unknown:
            raise StoreError(f"unknown photo field(s): {', '.join(unknown)}")
        raw = entry.get("path")
        if not isinstance(raw, str) or not raw.strip():
            raise StoreError("each photo needs a non-empty path")
        resolved = Path(raw).resolve()
        if root != resolved and root not in resolved.parents:
            raise StoreError(
                "photo paths must be inside the media store — import the file first with "
                "import_photos"
            )
        photo: dict = {"path": str(resolved)}
        uploaded = entry.get("uploaded_url")
        if uploaded is not None:
            if not isinstance(uploaded, str) or not uploaded.strip():
                raise StoreError("uploaded_url must be a non-empty string when present")
            photo["uploaded_url"] = uploaded
        out.append(photo)
    return out


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


def _thread_from_row(row: sqlite3.Row) -> ThreadSummary:
    """The thread record — identity, status, cursor, side-specific fields. Never a floor/budget."""
    return {name: row[name] for name in _THREAD_FIELDS}  # type: ignore[return-value]


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


def _want_from_row(row: sqlite3.Row) -> WantRecord:
    """The buyer-safe want view — never the max budget (that lives only behind the engine)."""
    record = {name: row[name] for name in _WANT_FIELDS}
    record["candidates"] = json.loads(row["candidates"])
    record["shortlist"] = json.loads(row["shortlist"])
    return record  # type: ignore[return-value]


_DEFAULT_CHANNEL: ChannelRecord = {
    "adapter": "telegram",
    "bot_username": None,
    "chat_id": None,
    "update_offset": 0,
    "bind_nonce": None,
    "welcomed_at": None,
    "commands_hash": None,
    "bound_ts": None,
    "updated_ts": None,
}


def _channel_from_row(row: sqlite3.Row) -> ChannelRecord:
    return {
        "adapter": row["adapter"],
        "bot_username": row["bot_username"],
        "chat_id": row["chat_id"],
        "update_offset": row["update_offset"],
        "bind_nonce": row["bind_nonce"],
        "welcomed_at": row["welcomed_at"],
        "commands_hash": row["commands_hash"],
        "bound_ts": row["bound_ts"],
        "updated_ts": row["updated_ts"],
    }


def _inbox_from_row(row: sqlite3.Row) -> InboxRecord:
    return {
        "id": row["id"],
        "event_id": row["event_id"],
        "kind": row["kind"],
        "text": row["text"],
        "payload": json.loads(row["payload"]) if row["payload"] else {},
        "media_paths": json.loads(row["media_paths"]) if row["media_paths"] else [],
        "src_ts": row["src_ts"],
        "received_ts": row["received_ts"],
        "status": row["status"],
        "handled_by": row["handled_by"],
        "pass_id": row["pass_id"],
        "updated_ts": row["updated_ts"],
    }


def _notice_from_row(row: sqlite3.Row) -> NoticeRecord:
    return {
        "id": row["id"],
        "text": row["text"],
        "ref": row["ref"],
        "created_ts": row["created_ts"],
        "status": row["status"],
        "attempts": row["attempts"],
        "delivered_ts": row["delivered_ts"],
        "via": row["via"],
        "pass_id": row["pass_id"],
        "holdable": bool(row["holdable"]),
        "controls": json.loads(row["controls"]) if row["controls"] else None,
    }


class PendingChangeRecord(TypedDict):
    """One row of the settings change ledger (see `_pending_change_from_row`). value/prior_value are
    the canonical values decoded from JSON; a live proposal is status 'pending'."""

    change_id: str
    key: str
    value: object
    prior_value: object
    status: str
    proposed_ts: float
    decided_ts: float | None
    decided_via: str | None


def _pending_change_from_row(row: sqlite3.Row) -> PendingChangeRecord:
    return {
        "change_id": row["change_id"],
        "key": row["key"],
        "value": json.loads(row["value"]),
        "prior_value": json.loads(row["prior_value"]) if row["prior_value"] is not None else None,
        "status": row["status"],
        "proposed_ts": row["proposed_ts"],
        "decided_ts": row["decided_ts"],
        "decided_via": row["decided_via"],
    }


def _insert_notice(
    conn,
    text: str,
    *,
    ref: str | None = None,
    pass_id: str | None = None,
    holdable: bool = False,
    controls: list | None = None,
) -> int:
    """Insert one queued notice within an existing transaction, returning its id. Shared by the
    standalone queue_notice and the settings apply paths (whose notice insert rides in the same
    transaction as the state change)."""
    cur = conn.execute(
        "INSERT INTO notices "
        "(text, ref, created_ts, status, attempts, pass_id, holdable, controls) "
        "VALUES (?, ?, ?, 'queued', 0, ?, ?, ?)",
        (
            text,
            ref,
            _now(),
            pass_id,
            1 if holdable else 0,
            json.dumps(controls, sort_keys=True) if controls is not None else None,
        ),
    )
    notice_id = cur.lastrowid
    assert notice_id is not None  # an INSERT always sets lastrowid
    return notice_id


def _json(value: object) -> str:
    """Canonical JSON for a stored setting/proposal value — the same encoding settings.py compares
    on, so a round-tripped value is byte-identical."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _upsert_setting_in_txn(conn, key: str, value: object, prior_value: object, now: float) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_ts, prior_value, prior_ts) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_ts = excluded.updated_ts, "
        "prior_value = excluded.prior_value, prior_ts = excluded.prior_ts",
        (key, _json(value), now, _json(prior_value), now),
    )


def _supersede_pending_in_txn(conn, key: str, now: float) -> None:
    """Retire any live proposal for a key — one live proposal per key, so a new proposal or an
    apply leaves the decision surface unambiguous."""
    conn.execute(
        "UPDATE pending_setting_changes SET status = 'superseded', decided_ts = ? "
        "WHERE key = ? AND status = 'pending'",
        (now, key),
    )


def _decide_pending_in_txn(conn, change_id: str, status: str, now: float, decided_via) -> None:
    conn.execute(
        "UPDATE pending_setting_changes SET status = ?, decided_ts = ?, decided_via = ? "
        "WHERE change_id = ?",
        (status, now, decided_via, change_id),
    )


_ESCALATION_FIELDS = (
    "id",
    "thread_id",
    "side",
    "item_id",
    "want_id",
    "kind",
    "open_question",
    "context_summary",
    "status",
    "resolution",
    "created_ts",
    "resolved_ts",
)


def _escalation_from_row(row: sqlite3.Row) -> EscalationRecord:
    return {name: row[name] for name in _ESCALATION_FIELDS}  # type: ignore[return-value]


class Store:
    """Typed access to selly.db, serialized behind the single write connection."""

    def __init__(self, db: Database):
        self._db = db

    # --- items: reads -----------------------------------------------------------------------

    def get_item(self, item_id: str) -> ItemRecord | None:
        rows = self._db.query("SELECT * FROM items WHERE id = ?", (item_id,))
        return _item_from_row(rows[0]) if rows else None

    def list_items(self, status: str | None = None) -> list[ItemRecord]:
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
        photos: list | None = None,
    ) -> ItemRecord:
        if not title or not title.strip():
            raise StoreError("title must be non-empty")
        stored_photos = validate_photos(photos or [])
        item_id = _new_id("item")
        ts = _now()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO items "
                "(id, title, description, condition, list_price, currency, status, "
                " listing_urls, photos, created_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, 'draft', '{}', ?, ?, ?)",
                (
                    item_id,
                    title.strip(),
                    description or "",
                    condition,
                    list_price,
                    currency,
                    json.dumps(stored_photos),
                    ts,
                    ts,
                ),
            )
        return self.get_item(item_id)  # type: ignore[return-value]

    def update_item(self, item_id: str, fields: dict) -> ItemRecord:
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
        if "photos" in fields:
            fields = dict(fields, photos=json.dumps(validate_photos(fields["photos"])))

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

    def set_photo_uploads(self, item_id: str, uploaded_urls: list) -> ItemRecord:
        """Stamp the uploaded media reference onto every photo of an item, in display order.

        All-or-nothing by shape: the caller passes one reference per photo and the whole set is
        written in one transaction. A partially-stamped set is the bug this guards against — the
        marketplace replaces an item's photo set wholesale, so publishing a half set ships the
        wrong cover.
        """
        with self._db.transaction() as conn:
            row = conn.execute("SELECT photos FROM items WHERE id = ?", (item_id,)).fetchone()
            if not row:
                raise ItemNotFound(f"no item with id {item_id!r}")
            photos = json.loads(row["photos"])
            if len(uploaded_urls) != len(photos):
                raise StoreError(
                    f"expected one uploaded url per photo ({len(photos)}), got {len(uploaded_urls)}"
                )
            stamped = [dict(photo, uploaded_url=url) for photo, url in zip(photos, uploaded_urls)]
            conn.execute(
                "UPDATE items SET photos = ?, updated_ts = ? WHERE id = ?",
                (json.dumps(stamped), _now(), item_id),
            )
        return self.get_item(item_id)  # type: ignore[return-value]

    def record_listing_url(self, item_id: str, market: str, url: str) -> ItemRecord:
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

    def get_floor(self, item_id: str) -> FloorRecord | None:
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
    ) -> FloorAck:
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
    ) -> ThreadRecord:
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
    ) -> ThreadRecord | None:
        rows = self._db.query("SELECT * FROM threads WHERE thread_id = ?", (thread_id,))
        if not rows:
            return None
        message_count = self._db.query(
            "SELECT COUNT(*) AS n FROM thread_messages WHERE thread_id = ?", (thread_id,)
        )[0]["n"]
        record: ThreadRecord = {
            **_thread_from_row(rows[0]),
            "messages": self.get_thread_messages(thread_id, limit=message_cap),
            "message_count": message_count,
        }
        return record

    def list_threads(
        self, side: str | None = None, status: str | None = None
    ) -> list[ThreadSummary]:
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

    def get_thread_messages(
        self, thread_id: str, *, limit: int | None = None
    ) -> list[MessageRecord]:
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

    def update_thread(self, thread_id: str, fields: dict) -> ThreadRecord:
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

    def hold_thread(
        self, thread_id: str, reason: str, mark_handled_msg: str | None = None
    ) -> ThreadRecord:
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

    def release_thread(self, thread_id: str) -> ThreadRecord:
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
        self,
        item_id: str,
        thread_id: str,
        handle: str,
        offer: float,
        *,
        config,
        firmness: str | None = None,
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
            knobs = negotiate_engine.resolve_knobs(config, floor_record, firmness)

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

    # --- checkout ---------------------------------------------------------------------------

    def checkout_floor_gate(self, item_id: str, price: float) -> dict:
        """The floor gate for a checkout close, returning only a status — never the floor value.
        Floorless orchestration lives here: at/above list persists the documented default floor;
        below list returns no_floor (ask the seller); below the floor returns below_floor."""
        with self._db.transaction() as conn:
            item = conn.execute(
                "SELECT list_price, currency FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if not item:
                raise ItemNotFound(f"no item with id {item_id!r}")
            list_price, currency = item["list_price"], item["currency"]
            floor_row = conn.execute(
                "SELECT floor FROM floors WHERE item_id = ?", (item_id,)
            ).fetchone()
            if floor_row is None:
                if not isinstance(list_price, (int, float)) or list_price <= 0:
                    raise StoreError(f"item {item_id!r} has no valid list price")
                if price < list_price:
                    return {"status": "no_floor", "currency": currency}
                conn.execute(
                    "INSERT INTO floors (item_id, floor, currency, source, updated_ts) "
                    "VALUES (?, ?, ?, 'default', ?)",
                    (item_id, list_price, currency, _now()),
                )
                floor = list_price
            else:
                floor = floor_row["floor"]
            if price < floor:
                return {"status": "below_floor", "currency": currency}
            return {"status": "ok", "currency": currency}

    def get_checkout(self, sale_id: str) -> CheckoutRecord | None:
        rows = self._db.query("SELECT * FROM checkouts WHERE sale_id = ?", (sale_id,))
        if not rows:
            return None
        row = rows[0]
        return {
            "sale_id": row["sale_id"],
            "item_id": row["item_id"],
            "thread_id": row["thread_id"],
            "checkout_url": row["checkout_url"],
            "price": row["price"],
            "currency": row["currency"],
            "issued_ts": row["issued_ts"],
        }

    def record_checkout(
        self, *, sale_id: str, item_id: str, thread_id: str, checkout_url: str, price, currency
    ) -> CheckoutRecord:
        """Persist the checkout record idempotently — the deterministic sale_id maps to one record;
        a re-record returns the existing one, never a second link."""
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO checkouts "
                "(sale_id, item_id, thread_id, checkout_url, price, currency, issued_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sale_id, item_id, thread_id, checkout_url, price, currency, _now()),
            )
        return self.get_checkout(sale_id)  # type: ignore[return-value]

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

    # --- send bracket -----------------------------------------------------------------------

    def reserve_reply(
        self,
        *,
        thread_id: str,
        kind: str,
        text: str,
        in_msg_id: str | None,
        cfg,
        now: float | None = None,
        interactive: bool = False,
    ) -> dict:
        """Transaction A of the send bracket: pacing reserve + (only on `go`) a durable intent, in
        one transaction. A wait/quiet verdict records no pacing action and no intent — a blocked
        reply leaves nothing behind for a sweep to re-drive. Returns the verdict and, on go, the
        intent id; the caller performs the sink send outside this transaction."""
        now = now if now is not None else _now()
        with self._db.transaction() as conn:
            thread = conn.execute(
                "SELECT market FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if not thread:
                raise ThreadNotFound(f"no thread with id {thread_id!r}")
            marketplace = thread["market"]
            cutoff = now - pacing_engine.WINDOW_SECONDS
            rows = conn.execute(
                "SELECT ts FROM pacing_actions WHERE marketplace = ? AND ts > ?",
                (marketplace, cutoff),
            ).fetchall()
            verdict = pacing_engine.evaluate(
                [r["ts"] for r in rows], now=now, cfg=cfg, kind=kind, interactive=interactive
            )
            if not verdict["record"]:
                return {"verdict": verdict["verdict"], "delay_sec": verdict["delay_sec"]}
            conn.execute(
                "INSERT INTO pacing_actions (marketplace, kind, ts) VALUES (?, ?, ?)",
                (marketplace, kind, now),
            )
            conn.execute(
                "DELETE FROM pacing_actions WHERE marketplace = ? AND ts <= ?",
                (marketplace, cutoff),
            )
            intent_id = _new_id("intent")
            conn.execute(
                "INSERT INTO send_intents "
                "(intent_id, thread_id, in_msg_id, text, kind, status, created_ts) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (intent_id, thread_id, in_msg_id, text, kind, now),
            )
            return {
                "verdict": "go",
                "delay_sec": verdict["delay_sec"],
                "intent_id": intent_id,
            }

    def commit_reply(
        self,
        *,
        intent_id: str,
        thread_id: str,
        in_msg_id: str | None,
        text: str,
        kind: str,
        now: float | None = None,
    ) -> dict:
        """Transaction B: fold the outbound row (a deterministic msg_id from the intent id makes a
        retried commit a UNIQUE no-op), advance the cursor over the handled inbound, mark the intent
        committed, and stamp follow-up state — all in one transaction."""
        now = now if now is not None else _now()
        out_msg_id = f"out|{intent_id}"
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO thread_messages "
                "(thread_id, msg_id, dir, text, ts, source) VALUES (?, ?, 'out', ?, ?, 'agent')",
                (thread_id, out_msg_id, text, now),
            )
            conn.execute(
                "UPDATE send_intents SET status = 'committed', sent_ts = ?, committed_ts = ? "
                "WHERE intent_id = ?",
                (now, now, intent_id),
            )
            if in_msg_id is not None:
                conn.execute(
                    "UPDATE threads SET cursor_last_msg_id = ?, cursor_last_ts = ?, "
                    "updated_ts = ? WHERE thread_id = ?",
                    (in_msg_id, now, now, thread_id),
                )
            if kind == "followup":
                conn.execute(
                    "UPDATE threads SET last_followup_ts = ?, followup_disposition = 'sent', "
                    "updated_ts = ? WHERE thread_id = ?",
                    (now, now, thread_id),
                )
        return {"msg_id": out_msg_id}

    def record_manual_reply(self, thread_id: str, text: str, *, handle: str | None = None) -> dict:
        """Journal a reply the seller sent themselves in the marketplace app: an outbound row,
        deduped by normalized text, with NO cursor advance and no status change (the manual send
        means our account spoke last, so follow-ups stop treating the buyer as unanswered)."""
        normalized = " ".join((text or "").split()).lower()
        msg_id = "manual|" + hashlib.sha256(normalized.encode()).hexdigest()[:12]
        with self._db.transaction() as conn:
            if not conn.execute(
                "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone():
                raise ThreadNotFound(f"no thread with id {thread_id!r}")
            cur = conn.execute(
                "INSERT OR IGNORE INTO thread_messages "
                "(thread_id, msg_id, dir, text, ts, source) VALUES (?, ?, 'out', ?, ?, 'manual')",
                (thread_id, msg_id, text, _now()),
            )
            return {"recorded": cur.rowcount > 0, "deduped": cur.rowcount == 0}

    def stale_intent_sweep(self, grace_sec: float, now: float | None = None) -> list[dict]:
        """Fold intents stuck un-committed past the grace window as `unconfirmed` and open an
        escalation to verify whether the send fired — never a re-send. The deterministic msg_id
        means a genuinely-retried commit is still a no-op, so this only ever heals a real stall."""
        now = now if now is not None else _now()
        cutoff = now - grace_sec
        folded: list[dict] = []
        with self._db.transaction() as conn:
            stale = conn.execute(
                "SELECT intent_id, thread_id FROM send_intents "
                "WHERE status IN ('pending', 'sent_unverified') AND created_ts < ?",
                (cutoff,),
            ).fetchall()
            for row in stale:
                conn.execute(
                    "UPDATE send_intents SET status = 'unconfirmed' WHERE intent_id = ?",
                    (row["intent_id"],),
                )
                esc_id, new = self._open_escalation_in_txn(
                    conn,
                    row["thread_id"],
                    open_question="verify whether this reply was actually sent",
                    kind="unconfirmed_send",
                )
                folded.append(
                    {
                        "intent_id": row["intent_id"],
                        "thread_id": row["thread_id"],
                        "escalation_id": esc_id,
                        "escalation_new": new,
                    }
                )
        return folded

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
            if not conn.execute(
                "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone():
                raise ThreadNotFound(f"no thread with id {thread_id!r}")
            esc_id, new = self._open_escalation_in_txn(
                conn,
                thread_id,
                open_question=open_question,
                kind=kind,
                context_summary=context_summary,
            )
            return {"id": esc_id, "idempotent": not new}

    def _open_escalation_in_txn(
        self, conn, thread_id: str, *, open_question: str, kind=None, context_summary=None
    ) -> tuple:
        """Open an escalation for a thread within an existing transaction (shared by escalate and
        the stale-intent sweep). Idempotent: an existing open escalation is returned unchanged.
        Returns (escalation_id, is_new)."""
        thread = conn.execute(
            "SELECT side, item_id, want_id FROM threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        existing = conn.execute(
            "SELECT id FROM escalations WHERE thread_id = ? AND status = 'open'", (thread_id,)
        ).fetchone()
        if existing:
            return existing["id"], False
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
        return esc_id, True

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

    def list_open_escalations(self) -> list[EscalationRecord]:
        """Every open escalation, oldest first — the needs-me read the catchup surface renders.
        Escalations clear only via resolve_escalation; a read never stamps them."""
        rows = self._db.query(
            "SELECT * FROM escalations WHERE status = 'open' ORDER BY created_ts ASC, id ASC"
        )
        return [_escalation_from_row(r) for r in rows]

    def get_escalation(self, escalation_id: str) -> EscalationRecord | None:
        rows = self._db.query("SELECT * FROM escalations WHERE id = ?", (escalation_id,))
        return _escalation_from_row(rows[0]) if rows else None

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

    # --- channel: the bound Telegram channel (singleton; states off/awaiting-bind/bound) --------

    def get_channel(self) -> ChannelRecord:
        """The channel singleton, or synthesized defaults when no row exists yet (off: no bot, no
        nonce, no chat, cursor 0). Never returns None so callers read a state, not an absence."""
        rows = self._db.query("SELECT * FROM channel WHERE id = 1")
        return _channel_from_row(rows[0]) if rows else dict(_DEFAULT_CHANNEL)  # type: ignore[return-value]

    def arm_bind(self, bot_username: str, bind_nonce: str) -> None:
        """Reset the channel row for a fresh bind and mint the nonce, one transaction: chat_id
        NULL, cursor 0, the connecting bot's username, the new nonce. welcomed_at and commands_hash
        survive only when the SAME bot re-binds — a re-connect must neither re-greet nor re-register
        commands; a different bot (a new token) resets both, so the new bot greets and registers."""
        now = _now()
        with self._db.transaction() as conn:
            existing = conn.execute("SELECT * FROM channel WHERE id = 1").fetchone()
            if existing is not None and existing["bot_username"] == bot_username:
                welcomed_at = existing["welcomed_at"]
                commands_hash = existing["commands_hash"]
            else:
                welcomed_at = None
                commands_hash = None
            conn.execute("DELETE FROM channel WHERE id = 1")
            conn.execute(
                "INSERT INTO channel "
                "(id, adapter, bot_username, chat_id, update_offset, bind_nonce, "
                " welcomed_at, commands_hash, bound_ts, updated_ts) "
                "VALUES (1, 'telegram', ?, NULL, 0, ?, ?, ?, NULL, ?)",
                (bot_username, bind_nonce, welcomed_at, commands_hash, now),
            )

    def complete_bind(self, chat_id: int, update_offset: int) -> None:
        """Bind the authorized chat: set chat_id, clear the nonce (single-use), stamp bound_ts, and
        advance the cursor past the /start — all one transaction, so a crash never leaves a half-
        bind. Raises if no row was armed (arm_bind must precede)."""
        now = _now()
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE channel SET chat_id = ?, bind_nonce = NULL, bound_ts = ?, "
                "update_offset = ?, updated_ts = ? WHERE id = 1",
                (chat_id, now, update_offset, now),
            )
            if cur.rowcount == 0:
                raise StoreError("no channel row to bind — arm_bind must run first")

    def advance_offset(self, update_offset: int) -> None:
        """Advance the Telegram cursor without ingesting — the awaiting-bind path acking (and thus
        discarding) unattributable pre-bind traffic. Never lowers the cursor."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE channel SET update_offset = ?, updated_ts = ? "
                "WHERE id = 1 AND ? > update_offset",
                (update_offset, _now(), update_offset),
            )

    def stamp_welcomed(self) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE channel SET welcomed_at = ?, updated_ts = ? WHERE id = 1", (_now(), _now())
            )

    def stamp_commands_hash(self, commands_hash: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE channel SET commands_hash = ?, updated_ts = ? WHERE id = 1",
                (commands_hash, _now()),
            )

    # --- channel inbox (durable intake; persist-then-ack in one transaction) --------------------

    def ingest_updates(self, events: list, update_offset: int) -> list[InboxRecord]:
        """Persist a batch of inbound updates and advance the cursor in ONE transaction (the next
        getUpdates offset silently acks the batch): acking and durability commit together, so a
        crash either re-delivers (deduped by event_id UNIQUE) or finds the rows already safe.
        Returns the rows actually inserted (new event_ids), arrival order, for the poller to act."""
        now = _now()
        inserted: list[InboxRecord] = []
        with self._db.transaction() as conn:
            for ev in events:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO channel_inbox "
                    "(event_id, kind, text, payload, media_paths, src_ts, received_ts, "
                    " status, updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (
                        ev["event_id"],
                        ev["kind"],
                        ev.get("text"),
                        json.dumps(ev.get("payload") or {}, sort_keys=True),
                        json.dumps(ev.get("media_paths") or []),
                        ev.get("src_ts"),
                        now,
                        now,
                    ),
                )
                if cur.rowcount:
                    row = conn.execute(
                        "SELECT * FROM channel_inbox WHERE id = ?", (cur.lastrowid,)
                    ).fetchone()
                    inserted.append(_inbox_from_row(row))
            conn.execute(
                "UPDATE channel SET update_offset = ?, updated_ts = ? WHERE id = 1",
                (update_offset, now),
            )
        return inserted

    def mark_inbox_handled(self, inbox_ids: list, handled_by: str) -> None:
        """Mark inbox rows handled by a deterministic fast path (never routed to a pass)."""
        if not inbox_ids:
            return
        placeholders = ",".join("?" for _ in inbox_ids)
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE channel_inbox SET status = 'handled', handled_by = ?, updated_ts = ? "
                f"WHERE id IN ({placeholders})",
                (handled_by, _now(), *inbox_ids),
            )

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

    def inbox_for_pass(self, pass_id: str) -> list[InboxRecord]:
        """The inbox rows claimed into a pass — the prompt builder reads these (arrival order)."""
        rows = self._db.query(
            "SELECT * FROM channel_inbox WHERE pass_id = ? ORDER BY id ASC", (pass_id,)
        )
        return [_inbox_from_row(r) for r in rows]

    def fold_inbox(self, pass_id: str, status: str) -> int:
        """Fold a channel pass's claimed rows to a terminal status: 'handled' (pass.end ok) or
        'failed' (error/timeout/paused — surfaced via a notice, never auto-refired)."""
        if status not in ("handled", "failed"):
            raise StoreError("fold_inbox status must be 'handled' or 'failed'")
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE channel_inbox SET status = ?, updated_ts = ? "
                "WHERE pass_id = ? AND status = 'claimed'",
                (status, _now(), pass_id),
            )
            return cur.rowcount

    def fold_settled_inbox(self, failure_notice: str) -> list[dict]:
        """Fold `claimed` inbox rows whose pass has already settled — handled when the pass
        finished ok, else failed plus one queued failure notice — all in one transaction.

        Everything derives from durable rows: a settled pass that still has claimed rows IS the
        signal, however the pass settled (a normal finish, a stale-swept crash, a kill between
        finishing and folding). No event delivery is required, so no crash shape can leave a
        seller's messages claimed forever. Idempotent: folded rows leave the claimed set, so a
        failed pass queues exactly one notice. Returns [{pass_id, status, rows}] per fold."""
        folded: list[dict] = []
        terminal = ",".join("?" for _ in _PASS_TERMINAL)
        with self._db.transaction() as conn:
            settled = conn.execute(
                "SELECT ci.pass_id, p.status AS pass_status, COUNT(*) AS n "
                "FROM channel_inbox ci JOIN passes p ON p.pass_id = ci.pass_id "
                f"WHERE ci.status = 'claimed' AND p.status IN ({terminal}) "
                "GROUP BY ci.pass_id, p.status",
                _PASS_TERMINAL,
            ).fetchall()
            now = _now()
            for row in settled:
                status = "handled" if row["pass_status"] == "done" else "failed"
                conn.execute(
                    "UPDATE channel_inbox SET status = ?, updated_ts = ? "
                    "WHERE pass_id = ? AND status = 'claimed'",
                    (status, now, row["pass_id"]),
                )
                if status == "failed":
                    conn.execute(
                        "INSERT INTO notices (text, ref, created_ts, status, attempts, pass_id) "
                        "VALUES (?, NULL, ?, 'queued', 0, ?)",
                        (failure_notice, now, row["pass_id"]),
                    )
                folded.append({"pass_id": row["pass_id"], "status": status, "rows": row["n"]})
        return folded

    def count_pending_inbox(self) -> int:
        rows = self._db.query("SELECT COUNT(*) AS n FROM channel_inbox WHERE status = 'pending'")
        return rows[0]["n"]

    def recent_transcript(self, limit: int) -> list[TranscriptEntry]:
        """The recent conversational window: inbound inbox rows (any status) interleaved with the
        agent's own outbound notices, ordered by the local clock, the most-recent `limit` entries
        oldest-first. A pure read of two already-durable tables — no new state — so a follow-up
        like "yes, do that" has the prior turn to resolve against."""
        inbox_rows = self._db.query(
            "SELECT text, kind, received_ts FROM channel_inbox "
            "ORDER BY received_ts DESC, id DESC LIMIT ?",
            (limit,),
        )
        notice_rows = self._db.query(
            "SELECT text, created_ts FROM notices ORDER BY created_ts DESC, id DESC LIMIT ?",
            (limit,),
        )
        entries: list[TranscriptEntry] = []
        for r in inbox_rows:
            text = r["text"] or ("[photo]" if r["kind"] == "photo" else "")
            entries.append(
                {"direction": "in", "kind": r["kind"], "text": text, "ts": r["received_ts"]}
            )
        for r in notice_rows:
            entries.append(
                {"direction": "out", "kind": "notice", "text": r["text"], "ts": r["created_ts"]}
            )
        entries.sort(key=lambda e: e["ts"])
        return entries[-limit:]

    # --- notices (the needs-me outbound queue: queued -> delivered) -----------------------------

    def queue_notice(
        self,
        text: str,
        *,
        ref: str | None = None,
        pass_id: str | None = None,
        holdable: bool = False,
        controls: list | None = None,
    ) -> int:
        with self._db.transaction() as conn:
            return _insert_notice(
                conn, text, ref=ref, pass_id=pass_id, holdable=holdable, controls=controls
            )

    def claim_queued_notices(self, limit: int, *, in_quiet: bool = False) -> list[NoticeRecord]:
        """The oldest queued notices, FIFO — the drain lane delivers them in order. During quiet
        hours (`in_quiet`) holdable notices are skipped so only seller-facing/immediate ones go out;
        they are claimed normally once the window ends."""
        where = "status = 'queued'" + (" AND holdable = 0" if in_quiet else "")
        rows = self._db.query(
            f"SELECT * FROM notices WHERE {where} ORDER BY created_ts ASC, id ASC LIMIT ?",
            (limit,),
        )
        return [_notice_from_row(r) for r in rows]

    def list_queued_notices(self) -> list[NoticeRecord]:
        """Every queued notice, FIFO — what catchup/status surface (catchup stamps them)."""
        rows = self._db.query(
            "SELECT * FROM notices WHERE status = 'queued' ORDER BY created_ts ASC, id ASC"
        )
        return [_notice_from_row(r) for r in rows]

    def mark_notice_delivered(self, notice_id: int, via: str) -> None:
        if via not in ("channel", "catchup"):
            raise StoreError("notice delivery via must be 'channel' or 'catchup'")
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE notices SET status = 'delivered', delivered_ts = ?, via = ? WHERE id = ?",
                (_now(), via, notice_id),
            )

    def bump_notice_attempts(self, notice_id: int) -> int:
        """Count a failed delivery try (the row stays queued, visible in catchup — loud, never
        silently dropped). Returns the new attempt count."""
        with self._db.transaction() as conn:
            conn.execute("UPDATE notices SET attempts = attempts + 1 WHERE id = ?", (notice_id,))
            row = conn.execute("SELECT attempts FROM notices WHERE id = ?", (notice_id,)).fetchone()
            return row["attempts"] if row else 0

    def count_queued_notices(self) -> int:
        rows = self._db.query("SELECT COUNT(*) AS n FROM notices WHERE status = 'queued'")
        return rows[0]["n"]

    # --- settings & the propose->approve->apply change ledger -----------------------------------
    #
    # The LLM only proposes; every apply happens here, in deterministic code, one transaction. The
    # registry (settings.py) owns validation, rendering, defaults, and the approval policy — the
    # store owns only durable state, so a caller passes concrete canonical values and the composed
    # notice copy in, and these methods persist the state change + its notice atomically.

    def get_setting(self, key: str) -> object | None:
        """One setting's stored canonical value, or None when unset (the registry default applies —
        defaults are not rows). Effective values are read via settings.get / settings.effective."""
        rows = self._db.query("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else None

    def get_all_settings(self) -> dict:
        """Every stored setting's canonical value, keyed by key (unset keys are simply absent)."""
        rows = self._db.query("SELECT key, value FROM settings")
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def get_pending_change(self, change_id: str) -> PendingChangeRecord | None:
        rows = self._db.query(
            "SELECT * FROM pending_setting_changes WHERE change_id = ?", (change_id,)
        )
        return _pending_change_from_row(rows[0]) if rows else None

    def list_pending_changes(self) -> list[PendingChangeRecord]:
        """Every live (pending) proposal, oldest first — what catchup and `settings list` surface
        (the CLI door needs the change id from somewhere)."""
        rows = self._db.query(
            "SELECT * FROM pending_setting_changes WHERE status = 'pending' "
            "ORDER BY proposed_ts ASC, change_id ASC"
        )
        return [_pending_change_from_row(r) for r in rows]

    def new_change_id(self) -> str:
        """Mint a change id up front, so a caller can encode it into an approval/echo notice's
        buttons before the proposal row and that notice are written in one transaction."""
        return _new_id("chg")

    def propose_setting_change(
        self,
        key: str,
        value: object,
        *,
        change_id: str,
        prior_value: object,
        notice_text: str,
        notice_controls: list | None = None,
        notice_ref: str | None = None,
    ) -> str:
        """HOLD path: write a pending proposal (superseding any live one for the same key) and queue
        its approval notice, atomically. Returns the change_id. The daemon — never the model —
        decided to hold; this only records that decision durably."""
        now = _now()
        with self._db.transaction() as conn:
            _supersede_pending_in_txn(conn, key, now)
            conn.execute(
                "INSERT INTO pending_setting_changes "
                "(change_id, key, value, prior_value, status, proposed_ts) "
                "VALUES (?, ?, ?, ?, 'pending', ?)",
                (change_id, key, _json(value), _json(prior_value), now),
            )
            _insert_notice(conn, notice_text, ref=notice_ref, controls=notice_controls)
        return change_id

    def apply_setting_now(
        self,
        key: str,
        value: object,
        *,
        change_id: str,
        prior_value: object,
        decided_via: str = "auto",
        notice_text: str | None = None,
        notice_controls: list | None = None,
        notice_ref: str | None = None,
    ) -> dict:
        """ALLOW path: record the proposal already applied, upsert the setting, and (when a notice
        is given) queue the echo notice — one transaction. Returns {change_id, key, value,
        prior_value}."""
        now = _now()
        with self._db.transaction() as conn:
            _supersede_pending_in_txn(conn, key, now)
            conn.execute(
                "INSERT INTO pending_setting_changes "
                "(change_id, key, value, prior_value, status, proposed_ts, decided_ts, "
                "decided_via) "
                "VALUES (?, ?, ?, ?, 'applied', ?, ?, ?)",
                (change_id, key, _json(value), _json(prior_value), now, now, decided_via),
            )
            _upsert_setting_in_txn(conn, key, value, prior_value, now)
            if notice_text is not None:
                _insert_notice(conn, notice_text, ref=notice_ref, controls=notice_controls)
        return {"change_id": change_id, "key": key, "value": value, "prior_value": prior_value}

    def approve_setting_change(
        self,
        change_id: str,
        *,
        decided_via: str,
        notice_text: str | None = None,
        notice_controls: list | None = None,
        notice_ref: str | None = None,
        ttl_sec: float = 0.0,
    ) -> dict:
        """Apply a held proposal through a door: upsert the setting from the proposal's snapshot,
        mark it applied, and (when a notice is given) queue the echo notice — one transaction,
        re-checking the row is still pending inside it (so a raced supersede/expiry never
        double-applies). Returns a status dict: applied | expired | not_pending (with the row's
        current status)."""
        now = _now()
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM pending_setting_changes WHERE change_id = ?", (change_id,)
            ).fetchone()
            if row is None:
                return {"status": "not_pending", "current": None}
            if row["status"] != "pending":
                return {"status": "not_pending", "current": row["status"]}
            if ttl_sec and now - row["proposed_ts"] > ttl_sec:
                _decide_pending_in_txn(conn, change_id, "expired", now, None)
                return {"status": "expired", "key": row["key"]}
            key = row["key"]
            value = json.loads(row["value"])
            prior_value = json.loads(row["prior_value"]) if row["prior_value"] is not None else None
            _upsert_setting_in_txn(conn, key, value, prior_value, now)
            _decide_pending_in_txn(conn, change_id, "applied", now, decided_via)
            if notice_text is not None:
                _insert_notice(conn, notice_text, ref=notice_ref, controls=notice_controls)
        return {
            "status": "applied",
            "change_id": change_id,
            "key": key,
            "value": value,
            "prior_value": prior_value,
        }

    def cancel_setting_change(
        self, change_id: str, *, decided_via: str, ttl_sec: float = 0.0
    ) -> dict:
        """Cancel a held proposal through a door (leaves the setting untouched). Returns a status
        dict: cancelled | expired | not_pending."""
        now = _now()
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM pending_setting_changes WHERE change_id = ?", (change_id,)
            ).fetchone()
            if row is None:
                return {"status": "not_pending", "current": None}
            if row["status"] != "pending":
                return {"status": "not_pending", "current": row["status"]}
            if ttl_sec and now - row["proposed_ts"] > ttl_sec:
                _decide_pending_in_txn(conn, change_id, "expired", now, None)
                return {"status": "expired", "key": row["key"]}
            _decide_pending_in_txn(conn, change_id, "cancelled", now, decided_via)
        return {"status": "cancelled", "change_id": change_id, "key": row["key"]}

    def undo_setting_change(
        self,
        change_id: str,
        *,
        decided_via: str,
        notice_text: str | None = None,
        notice_controls: list | None = None,
        notice_ref: str | None = None,
    ) -> dict:
        """Revert an applied change through a door: restore its prior value via the same apply
        transaction (a fresh applied ledger row +, when a notice is given, a confirmation notice).
        Valid only while it is still the key's latest change — a later change to the key makes it
        stale. Returns a status dict: undone | not_undoable (with a reason)."""
        now = _now()
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM pending_setting_changes WHERE change_id = ?", (change_id,)
            ).fetchone()
            if row is None:
                return {"status": "not_undoable", "reason": "unknown"}
            if row["status"] != "applied":
                return {"status": "not_undoable", "reason": "not_applied", "current": row["status"]}
            key = row["key"]
            current = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            # Undo is single-level: valid only while this change is still the key's latest, i.e. the
            # stored value is still exactly what it set. A newer change moved it — refuse (stale).
            if current is None or current["value"] != row["value"]:
                return {"status": "not_undoable", "reason": "superseded"}
            restored = json.loads(row["prior_value"]) if row["prior_value"] is not None else None
            replaced = json.loads(row["value"])
            revert_id = _new_id("chg")
            conn.execute(
                "INSERT INTO pending_setting_changes "
                "(change_id, key, value, prior_value, status, proposed_ts, decided_ts, "
                "decided_via) "
                "VALUES (?, ?, ?, ?, 'applied', ?, ?, ?)",
                (revert_id, key, _json(restored), _json(replaced), now, now, decided_via),
            )
            _upsert_setting_in_txn(conn, key, restored, replaced, now)
            if notice_text is not None:
                _insert_notice(conn, notice_text, ref=notice_ref, controls=notice_controls)
        return {
            "status": "undone",
            "change_id": revert_id,
            "key": key,
            "value": restored,
            "prior_value": replaced,
        }

    def expire_pending_changes(self, cutoff_ts: float) -> list[PendingChangeRecord]:
        """Mark every pending proposal older than the cutoff expired, returning those rows (the
        expiry sweep publishes an event per one). An expired proposal is answered, never re-fired —
        the seller starts fresh."""
        now = _now()
        with self._db.transaction() as conn:
            stale = conn.execute(
                "SELECT * FROM pending_setting_changes "
                "WHERE status = 'pending' AND proposed_ts < ?",
                (cutoff_ts,),
            ).fetchall()
            for row in stale:
                _decide_pending_in_txn(conn, row["change_id"], "expired", now, None)
        return [_pending_change_from_row(r) for r in stale]

    # --- pause control (singleton; a missing row reads as NOT paused) ---------------------------

    def set_paused(self, paused: bool, *, source: str | None = None) -> None:
        """Set/clear the pause flag. since_ts is stamped only on the false->true edge (a redundant
        pause keeps the original), and cleared on resume."""
        with self._db.transaction() as conn:
            existing = conn.execute("SELECT paused, since_ts FROM control WHERE id = 1").fetchone()
            was_paused = bool(existing["paused"]) if existing else False
            if paused:
                since = existing["since_ts"] if (was_paused and existing) else _now()
            else:
                since = None
            conn.execute(
                "INSERT INTO control (id, paused, since_ts, source) VALUES (1, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET paused = excluded.paused, "
                "since_ts = excluded.since_ts, source = excluded.source",
                (1 if paused else 0, since, source),
            )

    def is_paused(self) -> bool:
        """Missing row reads as NOT paused — a corrupt/absent control state can never strand the
        agent paused forever (fail toward not-paused)."""
        rows = self._db.query("SELECT paused FROM control WHERE id = 1")
        return bool(rows[0]["paused"]) if rows else False


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
    "set_photo_uploads": (("item_id", "item"),),
    "get_thread": (("thread_id", "thread"),),
    "get_thread_messages": (("thread_id", "thread"),),
    "append_thread_message": (("thread_id", "thread"),),
    "update_thread": (("thread_id", "thread"),),
    "hold_thread": (("thread_id", "thread"),),
    "release_thread": (("thread_id", "thread"),),
    "escalate": (("thread_id", "thread"),),
    "checkout_floor_gate": (("item_id", "item"),),
    "reserve_reply": (("thread_id", "thread"),),
    "commit_reply": (("thread_id", "thread"),),
    "record_manual_reply": (("thread_id", "thread"),),
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
    "set_photo_uploads": ("item", ItemNotFound),
    "append_thread_message": ("thread", ThreadNotFound),
    "update_thread": ("thread", ThreadNotFound),
    "hold_thread": ("thread", ThreadNotFound),
    "release_thread": ("thread", ThreadNotFound),
    "escalate": ("thread", ThreadNotFound),
    "checkout_floor_gate": ("item", ItemNotFound),
    "reserve_reply": ("thread", ThreadNotFound),
    "commit_reply": ("thread", ThreadNotFound),
    "record_manual_reply": ("thread", ThreadNotFound),
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
    def list_threads(
        self, side: str | None = None, status: str | None = None
    ) -> list[ThreadSummary]:
        rows = self._store.list_threads(side=side, status=status)
        if self._scope is None:
            return rows
        return [r for r in rows if r["thread_id"] in self._scope.thread_ids]

    def list_items(self, status: str | None = None) -> list[ItemRecord]:
        rows = self._store.list_items(status=status)
        if self._scope is None:
            return rows
        return [r for r in rows if r["id"] in self._scope.item_ids]

    def list_wants(self, status: str | None = None) -> list[WantRecord]:
        rows = self._store.list_wants(status=status)
        if self._scope is None:
            return rows
        return [r for r in rows if r["want_id"] in self._scope.want_ids]

    def __getattr__(self, name: str):
        target = getattr(self._store, name)
        spec = _SCOPE_GUARDED.get(name)
        scope = self._scope
        if scope is None or spec is None:
            return target
        sig = inspect.signature(target)

        def guarded(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            for param, kind in spec:
                if not scope.allows(kind, bound.arguments.get(param)):
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
