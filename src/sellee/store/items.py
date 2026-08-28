"""Items, photos, listing URLs, the Q&A bank, the selector cache, and floors."""

from __future__ import annotations

import json

from sellee.db import Database
from sellee.store.helpers import (
    _FLOOR_SOURCES,
    _ITEM_STATUSES,
    _ITEM_WRITABLE,
    _QA_SEARCH_CAP,
    _QA_SOURCES,
    _UI_CACHE_STRATEGIES,
    QA_GLOBAL_ITEM,
    FloorAck,
    FloorRecord,
    ItemNotFound,
    ItemRecord,
    StoreError,
    _insert_item_in_txn,
    _item_from_row,
    _like_escape,
    _now,
    _ui_cache_from_row,
    validate_photos,
)


class ItemsMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

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
        ts = _now()
        with self._db.transaction() as conn:
            item_id = _insert_item_in_txn(
                conn,
                title=title,
                list_price=list_price,
                currency=currency,
                description=description,
                condition=condition,
                photos=stored_photos,
                now=ts,
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
        """Merge one verified listing URL into the item's listing_urls map — a live verify has
        already passed before this is called.

        The only writer that adds a URL to an item that already exists. The others are
        `archive_listing_url` below, and `adopt_discovered_listing`, which writes the URL in the
        same INSERT as the item so the two cannot be separated by a crash.
        """
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

    # --- Q&A bank ---------------------------------------------------------------------------

    def qa_add(self, item_id: str, question: str, answer: str, source: str) -> dict:
        """Bank one taught answer. item_id '*' is a global entry."""
        if source not in _QA_SOURCES:
            raise StoreError(f"qa source must be one of {_QA_SOURCES}")
        if not (question or "").strip() or not (answer or "").strip():
            raise StoreError("question and answer must both be non-empty")
        if item_id != QA_GLOBAL_ITEM and self.get_item(item_id) is None:
            raise ItemNotFound(f"no item with id {item_id!r}")
        ts = _now()
        with self._db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO qa_bank (item_id, question, answer, source, created_ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (item_id, question.strip(), answer.strip(), source, ts),
            )
            entry_id = cur.lastrowid
        return {"id": entry_id, "item_id": item_id, "source": source, "created_ts": ts}

    def qa_search(self, item_id: str, query: str | None = None, limit: int = _QA_SEARCH_CAP):
        """The item's entries plus the global ones, newest first, capped.

        Matching is a deliberately dumb substring filter over question and answer: the rows come
        back for the LLM to match semantically, so a smarter index would only narrow what it can
        see. An empty/absent query returns the whole (capped) bank for the item.
        """
        limit = max(1, min(int(limit), _QA_SEARCH_CAP))
        params: list = [item_id, QA_GLOBAL_ITEM]
        sql = "SELECT * FROM qa_bank WHERE item_id IN (?, ?)"
        needle = (query or "").strip()
        if needle:
            sql += " AND (question LIKE ? ESCAPE '\\' OR answer LIKE ? ESCAPE '\\')"
            like = f"%{_like_escape(needle)}%"
            params += [like, like]
        sql += " ORDER BY created_ts DESC, id DESC LIMIT ?"
        params.append(limit)
        return [
            {
                "id": r["id"],
                "item_id": r["item_id"],
                "question": r["question"],
                "answer": r["answer"],
                "source": r["source"],
                "created_ts": r["created_ts"],
            }
            for r in self._db.query(sql, tuple(params))
        ]

    # --- selector cache ("page memory") -----------------------------------------------------
    #
    # An acceleration layer over the browser flows, never a decision input: a hit says only WHERE a
    # control was last found.

    def ui_cache_get(self, market: str, flow: str, step: str | None = None) -> dict:
        """One step's cached selector, or the whole flow's map when no step is named (the batched
        preplan read). Each entry carries a derived `stale` flag the caller treats as a miss."""
        now = _now()
        if step is not None:
            rows = self._db.query(
                "SELECT * FROM ui_cache WHERE market = ? AND flow = ? AND step = ?",
                (market, flow, step),
            )
            entry = _ui_cache_from_row(rows[0], now) if rows else None
            return {
                "market": market,
                "flow": flow,
                "step": step,
                "hit": entry is not None,
                "stale": True if entry is None else entry["stale"],
                "selector": entry,
            }
        rows = self._db.query(
            "SELECT * FROM ui_cache WHERE market = ? AND flow = ? ORDER BY step ASC",
            (market, flow),
        )
        steps = {r["step"]: _ui_cache_from_row(r, now) for r in rows}
        return {"market": market, "flow": flow, "hit": bool(steps), "steps": steps}

    def ui_cache_record(
        self,
        *,
        market: str,
        flow: str,
        step: str,
        strategy: str,
        query: str,
        page_url_pattern: str,
        action_kind: str = "",
    ) -> dict:
        """Upsert a verified selector — the self-heal entry point. Refuses a row with no page-URL
        guard: without one the selector would be resolved on whatever page happened to be open, so
        it could never be trusted and would simply read as permanently stale. Recording resets
        fail_count (this row just worked) and preserves the original recorded_at."""
        if strategy not in _UI_CACHE_STRATEGIES:
            raise StoreError(f"ui cache strategy must be one of {_UI_CACHE_STRATEGIES}")
        if not (query or "").strip():
            raise StoreError("a ui cache row needs a non-empty query")
        if not (page_url_pattern or "").strip():
            raise StoreError("a ui cache row needs a page_url_pattern")
        now = _now()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO ui_cache "
                "(market, flow, step, strategy, query, action_kind, page_url_pattern, "
                " recorded_at, last_verified_at, last_ok_at, fail_count, ok_streak) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1) "
                "ON CONFLICT (market, flow, step) DO UPDATE SET "
                "strategy = excluded.strategy, query = excluded.query, "
                "action_kind = excluded.action_kind, "
                "page_url_pattern = excluded.page_url_pattern, "
                "last_verified_at = excluded.last_verified_at, "
                "last_ok_at = excluded.last_ok_at, fail_count = 0, "
                "ok_streak = ui_cache.ok_streak + 1",
                (
                    market,
                    flow,
                    step,
                    strategy,
                    query.strip(),
                    action_kind,
                    page_url_pattern.strip(),
                    now,
                    now,
                    now,
                ),
            )
        return {"market": market, "flow": flow, "step": step, "recorded": True}

    def ui_cache_invalidate(self, market: str, flow: str, step: str | None = None) -> dict:
        """Drop a step (or the whole flow) after a miss or a failed verify, so the next pass
        re-finds it by vision and re-records what it found."""
        with self._db.transaction() as conn:
            if step is None:
                cur = conn.execute(
                    "DELETE FROM ui_cache WHERE market = ? AND flow = ?", (market, flow)
                )
            else:
                cur = conn.execute(
                    "DELETE FROM ui_cache WHERE market = ? AND flow = ? AND step = ?",
                    (market, flow, step),
                )
            return {"market": market, "flow": flow, "step": step, "removed": cur.rowcount}

    def ui_cache_fail(self, market: str, flow: str, step: str) -> dict:
        """Count a failed resolve against a step. Three failures make it stale, so a selector the
        page has moved away from stops being offered without needing an explicit invalidate."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE ui_cache SET fail_count = fail_count + 1, ok_streak = 0 "
                "WHERE market = ? AND flow = ? AND step = ?",
                (market, flow, step),
            )
            row = conn.execute(
                "SELECT fail_count FROM ui_cache WHERE market = ? AND flow = ? AND step = ?",
                (market, flow, step),
            ).fetchone()
        return {"step": step, "fail_count": row["fail_count"] if row else 0}

    def archive_listing_url(self, item_id: str, market: str) -> ItemRecord:
        """Drop one market's URL from the item's listing_urls — the listing is no longer live there.

        The counterpart of record_listing_url — the only writer that takes a URL away.
        """
        with self._db.transaction() as conn:
            row = conn.execute("SELECT listing_urls FROM items WHERE id = ?", (item_id,)).fetchone()
            if not row:
                raise ItemNotFound(f"no item with id {item_id!r}")
            urls = json.loads(row["listing_urls"])
            urls.pop(market, None)
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
