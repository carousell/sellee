"""Shared plumbing for the store package: constants, errors, record types, row mappers, and
the transaction helpers. Everything here is imported by name into the domain mixins at import
time, so those bindings are fixed early — runtime patching must reach every module that carries
the name, which is what tests/conftest.py's patch_store_attr does.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from sellee import marketplaces, paths

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

# A Q&A search returns rows for the LLM to match semantically, so the cap is about prompt budget,
# not relevance: dozens of entries per item at most.
_QA_SEARCH_CAP = 20
QA_GLOBAL_ITEM = "*"
_QA_SOURCES = ("seller",)

# Sell threads a reply pass may be spawned for: a buyer is mid-conversation, not gone or handed
# over. A held/escalated thread is deliberately excluded — it is waiting on someone else.
_REPLY_THREAD_STATUSES = ("active", "liaising", "agreed")

UI_CACHE_STALE_FAILS = 3
UI_CACHE_STALE_DAYS = 30
_UI_CACHE_STRATEGIES = ("css", "aria", "role", "text")

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


def _like_escape(text: str) -> str:
    """Neutralize LIKE wildcards so a buyer's literal `%` searches for a `%`, not everything."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Sell threads whose buyer is still waiting on us. Shared by the read accessor and the enqueue
# transaction, which must run it on its own connection (the read helper takes the same DB lock).
#
# Two independent conditions have to hold, and they answer different questions.
#
# The cursor answers "have we handled this message". It advances only on a committed reply, and only
# as far as the messages the answering pass was given — so a crash between reading a buyer's message
# and answering it leaves the thread eligible, and so does a message that arrives mid-compose.
#
# The second answers "has the seller stepped in". A reply they typed in the marketplace app arrives
# as an outbound row on the next read, and talking over them is worse than staying quiet. It tests
# provenance rather than the last row's direction, because direction cannot tell that case from "we
# replied and the buyer has since said more" — the common one, which must stay eligible.
_UNHANDLED_INBOUND_SQL = (
    "SELECT t.thread_id, t.item_id, "
    "  (SELECT mw.msg_id FROM thread_messages mw WHERE mw.thread_id = t.thread_id "
    "     AND mw.dir = 'in' ORDER BY mw.ts DESC, mw.rowid DESC LIMIT 1) AS newest_in_msg_id, "
    "  (SELECT MAX(mx.ts) FROM thread_messages mx WHERE mx.thread_id = t.thread_id "
    "     AND mx.dir = 'in') AS newest_in_ts "
    "FROM threads t "
    "WHERE t.side = 'sell' AND t.status IN ({statuses}) "
    "AND EXISTS (SELECT 1 FROM thread_messages m WHERE m.thread_id = t.thread_id "
    "  AND m.dir = 'in' AND (t.cursor_last_ts IS NULL OR m.ts > t.cursor_last_ts)) "
    "AND NOT EXISTS (SELECT 1 FROM thread_messages ms WHERE ms.thread_id = t.thread_id "
    "  AND ms.dir = 'out' AND ms.source = 'manual' "
    "  AND ms.ts > (SELECT MAX(mi.ts) FROM thread_messages mi "
    "               WHERE mi.thread_id = t.thread_id AND mi.dir = 'in')) "
    "AND NOT EXISTS (SELECT 1 FROM escalations e WHERE e.thread_id = t.thread_id "
    "  AND e.status = 'open') "
    "ORDER BY t.thread_id ASC"
).format(statuses=",".join("?" for _ in _REPLY_THREAD_STATUSES))


def _unhandled_inbound_rows(rows) -> list[dict]:
    return [{"thread_id": r["thread_id"], "item_id": r["item_id"]} for r in rows]


def _claimed_through(rows) -> dict:
    """The newest buyer message per thread at claim time — how far a pass may advance the cursor.

    Recorded when the threads are claimed rather than read back at send time, because by then the
    buyer may have written again: advancing over that message would mark it handled by a pass that
    never saw it.
    """
    return {
        row["thread_id"]: [row["newest_in_msg_id"], row["newest_in_ts"]]
        for row in rows
        if row["newest_in_msg_id"] is not None
    }


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
    """One transcript row (see `get_thread_messages`). scam_verdict is stamped by the daemon's
    pre-scan on marketplace inbound; None on rows that arrived by another path."""

    msg_id: str
    dir: str
    text: str
    ts: float
    source: str | None
    scam_verdict: str | None


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


# `class` is a reserved word, so PassRecord needs the functional syntax — where `from __future__
# import annotations` does not apply, because the annotations are values in a call rather than
# annotations the compiler sees. They are written as strings for that reason; the checker still
# resolves them.
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
    token lives in its own 0600 file, never here).

    `bind_nonce_expires_ts` is the armed nonce's deadline — read it through `bind_nonce_live`
    rather than comparing it directly, so the NULL-is-expired rule stays in one place."""

    adapter: str
    bot_username: str | None
    chat_id: int | None
    update_offset: int
    bind_nonce: str | None
    bind_nonce_expires_ts: float | None
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
    media_paths: list
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


# The channel adapters that exist. `channel.adapter` carries no enumerating CHECK — widening one
# means recreating the table in SQLite — so this is where an adapter name is validated, on the way
# in through `arm_bind`. A guard test pins it against the daemon's actual provider map.
KNOWN_ADAPTERS = ("telegram", "discord")

# How long an armed bind nonce stays adoptable. The connect flow tells the seller to relay it to
# their phone, so a copy of it sits in a chat history — and an abandoned bind would leave the
# channel armed forever, since nothing else closes the window. 15 minutes is generous for getting
# a link onto a phone.
BIND_NONCE_TTL_SEC = 900

_DEFAULT_CHANNEL: ChannelRecord = {
    "adapter": "telegram",
    "bot_username": None,
    "chat_id": None,
    "update_offset": 0,
    "bind_nonce": None,
    "bind_nonce_expires_ts": None,
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
        "bind_nonce_expires_ts": row["bind_nonce_expires_ts"],
        "welcomed_at": row["welcomed_at"],
        "commands_hash": row["commands_hash"],
        "bound_ts": row["bound_ts"],
        "updated_ts": row["updated_ts"],
    }


def bind_nonce_live(ch: ChannelRecord, *, now: float | None = None) -> bool:
    """Whether the row's armed nonce can still bind a chat. The one reader of the expiry, so every
    caller (both providers' loops, both status routes) fails the same way.

    A missing deadline reads as expired, not as "no limit": those rows were armed before nonces had
    one, which is exactly the unbounded state this guards against."""
    if not ch["bind_nonce"]:
        return False
    expires_ts = ch["bind_nonce_expires_ts"]
    return expires_ts is not None and (_now() if now is None else now) < expires_ts


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
    transaction as the state change).

    Text is stored as written. Most notices are the agent's own words to its seller, and the one
    that relays buyer-derived text collapses it where it embeds it — see the escalation relay in
    channel/outbound.py.
    """
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


def _insert_item_in_txn(
    conn,
    *,
    title: str,
    list_price: float | None,
    currency: str | None,
    description: str,
    condition: str | None,
    photos: list,
    now: float,
    status: str = "draft",
    listing_urls: dict | None = None,
) -> str:
    """Insert one item within an existing transaction, returning its new id.

    Two callers, and the second is why this is a helper: `create_item` opens with an empty
    `listing_urls` and a draft status, while adoption must write the item **and** the marketplace
    URL it was read from in one statement — split in two, a crash leaves an item with no URL and a
    retry creates a second for the same listing.

    Validation belongs to the caller, before the transaction opens: `Database.transaction` is not
    reentrant, so raising in here would unwind a held write lock.
    """
    item_id = _new_id("item")
    conn.execute(
        "INSERT INTO items "
        "(id, title, description, condition, list_price, currency, status, "
        " listing_urls, photos, created_ts, updated_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item_id,
            title.strip(),
            description or "",
            condition,
            list_price,
            currency,
            status,
            json.dumps(listing_urls or {}, sort_keys=True),
            json.dumps(photos),
            now,
            now,
        ),
    )
    return item_id


def _stamp_welcomed_in_txn(conn) -> None:
    now = _now()
    conn.execute("UPDATE channel SET welcomed_at = ?, updated_ts = ? WHERE id = 1", (now, now))


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


def ui_cache_is_stale(entry: dict, now: float) -> bool:
    """Whether a cached selector must be treated as a miss: it has failed too often, carries no
    page-URL guard, or has not been re-verified inside the freshness window. Stale never means
    "act anyway" — the caller falls back to vision exactly as it would on a miss."""
    if entry.get("fail_count", 0) >= UI_CACHE_STALE_FAILS:
        return True
    if not (entry.get("page_url_pattern") or "").strip():
        return True
    last_verified = entry.get("last_verified_at")
    if last_verified is None:
        return True
    return (now - last_verified) > UI_CACHE_STALE_DAYS * 86400.0


def _ui_cache_from_row(row: sqlite3.Row, now: float) -> dict:
    entry = {
        "market": row["market"],
        "flow": row["flow"],
        "step": row["step"],
        "strategy": row["strategy"],
        "query": row["query"],
        "action_kind": row["action_kind"],
        "page_url_pattern": row["page_url_pattern"],
        "recorded_at": row["recorded_at"],
        "last_verified_at": row["last_verified_at"],
        "last_ok_at": row["last_ok_at"],
        "fail_count": row["fail_count"],
        "ok_streak": row["ok_streak"],
    }
    entry["stale"] = ui_cache_is_stale(entry, now)
    return entry


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
