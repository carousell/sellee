"""Event bus + transcript store — the one observability record (A11).

publish(kind, payload, pass_id) stamps the journal clock at write and appends to events.db;
that timestamp is the sole ordering key (INV-23). A caller never supplies the ordering time —
a transport's own clock, if any, rides inside payload. Live subscribers can register (the
seam the web tail plugs into later); today the store is the only sink. A subscriber that
raises never breaks a publish.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from selly_agent.db import Database

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    seq: int
    ts: float
    pass_id: str | None
    kind: str
    payload: dict


def _row_to_event(row) -> Event:
    return Event(
        seq=row["seq"],
        ts=row["ts"],
        pass_id=row["pass_id"],
        kind=row["kind"],
        payload=json.loads(row["payload"]),
    )


def event_to_wire(event: Event) -> dict:
    """Canonical JSON shape of an event: the `inspect --json` NDJSON line and the web tail's
    /events.json rows share this exactly. `@ts` is a system-local RFC3339 render of `ts`,
    emitted first for legibility; `ts` stays as the raw epoch (the faithful ordering value).
    The field order here is the wire order — serialize without sort_keys to preserve it."""
    return {
        "@ts": datetime.fromtimestamp(event.ts).astimezone().isoformat(),
        "seq": event.seq,
        "ts": event.ts,
        "pass_id": event.pass_id,
        "kind": event.kind,
        "payload": event.payload,
    }


def query_events(
    conn,
    *,
    after_seq: int | None = None,
    since_ts: float | None = None,
    pass_id: str | None = None,
    kinds: Iterable[str] | None = None,
    limit: int | None = None,
) -> list[Event]:
    """Read events from any connection (writer or a read-only reader), ordered by seq. The
    inspect CLI drives this over its own read-only connection, needing no daemon cooperation."""
    clauses: list[str] = []
    params: list = []
    if after_seq is not None:
        clauses.append("seq > ?")
        params.append(after_seq)
    if since_ts is not None:
        clauses.append("ts >= ?")
        params.append(since_ts)
    if pass_id is not None:
        clauses.append("pass_id = ?")
        params.append(pass_id)
    kinds = list(kinds) if kinds is not None else None
    if kinds:
        clauses.append(f"kind IN ({','.join('?' for _ in kinds)})")
        params.extend(kinds)
    sql = "SELECT seq, ts, pass_id, kind, payload FROM events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY seq ASC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [_row_to_event(r) for r in conn.execute(sql, tuple(params)).fetchall()]


class EventStore:
    """The durable sink: appends events to events.db, stamping ts at write."""

    def __init__(self, db: Database):
        self._db = db

    @property
    def db(self) -> Database:
        return self._db

    def record(self, kind: str, payload: dict, pass_id: str | None = None) -> Event:
        ts = time.time()  # the journal clock — assigned here, never by the caller
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        with self._db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO events (ts, pass_id, kind, payload) VALUES (?, ?, ?, ?)",
                (ts, pass_id, kind, payload_json),
            )
            seq = cur.lastrowid
        return Event(seq=seq, ts=ts, pass_id=pass_id, kind=kind, payload=payload)

    def read(self, **kwargs) -> list[Event]:
        with self._db._lock:  # noqa: SLF001 — the store owns this connection's serialization
            return query_events(self._db._conn, **kwargs)

    def delete_older_than(self, cutoff_ts: float, keep_kinds: Iterable[str]) -> int:
        keep = list(keep_kinds)
        with self._db.transaction() as conn:
            if keep:
                placeholders = ",".join("?" for _ in keep)
                cur = conn.execute(
                    f"DELETE FROM events WHERE ts < ? AND kind NOT IN ({placeholders})",
                    (cutoff_ts, *keep),
                )
            else:
                cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff_ts,))
            return cur.rowcount


Subscriber = Callable[[Event], None]


class EventBus:
    """In-process pub/sub over the store. publish writes to the store first, then fans the
    stored event (seq + ts assigned) out to any live subscribers."""

    def __init__(self, store: EventStore):
        self._store = store
        self._subscribers: list[Subscriber] = []
        self._lock = threading.Lock()

    @property
    def store(self) -> EventStore:
        return self._store

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def publish(self, kind: str, payload: dict, pass_id: str | None = None) -> Event:
        event = self._store.record(kind, payload, pass_id=pass_id)
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:  # a broken subscriber must never break a publish
                log.exception("event subscriber raised for %s", kind)
        return event
