"""The bound channel, its durable inbox, and the outbound notice queue."""

from __future__ import annotations

import json

from sellee.db import Database
from sellee.store.helpers import (
    _DEFAULT_CHANNEL,
    _PASS_TERMINAL,
    BIND_NONCE_TTL_SEC,
    KNOWN_ADAPTERS,
    ChannelRecord,
    InboxRecord,
    NoticeRecord,
    StoreError,
    TranscriptEntry,
    _channel_from_row,
    _inbox_from_row,
    _insert_notice,
    _notice_from_row,
    _now,
    _stamp_welcomed_in_txn,
)


class ChannelMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

    # --- channel: the bound Telegram channel (singleton; states off/awaiting-bind/bound) --------

    def get_channel(self) -> ChannelRecord:
        """The channel singleton, or synthesized defaults when no row exists yet (off: no bot, no
        nonce, no chat, cursor 0). Never returns None so callers read a state, not an absence."""
        rows = self._db.query("SELECT * FROM channel WHERE id = 1")
        return _channel_from_row(rows[0]) if rows else dict(_DEFAULT_CHANNEL)  # type: ignore[return-value]

    def arm_bind(
        self,
        bot_username: str,
        bind_nonce: str,
        *,
        adapter: str = "telegram",
        expires_ts: float | None = None,
    ) -> None:
        """Reset the channel row for a fresh bind and mint the nonce, one transaction: chat_id
        NULL, cursor 0, the connecting bot's username, the new nonce and its deadline. welcomed_at
        and commands_hash survive only when the SAME bot on the SAME adapter re-binds — a
        re-connect must neither re-greet nor re-register commands; a different bot, or a switch to
        the other provider, resets both, so the new bot greets and registers.

        `adapter` must name a known provider: this is the only write path to the column, so it is
        where an adapter typo is caught rather than silently binding to a provider that will never
        be started.

        `expires_ts` defaults to the standard TTL from now, so arming without one is impossible
        rather than merely discouraged — an immortal nonce is the failure this column exists for."""
        if adapter not in KNOWN_ADAPTERS:
            raise ValueError(f"unknown channel adapter: {adapter!r}")
        now = _now()
        if expires_ts is None:
            expires_ts = now + BIND_NONCE_TTL_SEC
        with self._db.transaction() as conn:
            existing = conn.execute("SELECT * FROM channel WHERE id = 1").fetchone()
            if (
                existing is not None
                and existing["bot_username"] == bot_username
                and existing["adapter"] == adapter
            ):
                welcomed_at = existing["welcomed_at"]
                commands_hash = existing["commands_hash"]
            else:
                welcomed_at = None
                commands_hash = None
            conn.execute("DELETE FROM channel WHERE id = 1")
            conn.execute(
                "INSERT INTO channel "
                "(id, adapter, bot_username, chat_id, update_offset, bind_nonce, "
                " bind_nonce_expires_ts, welcomed_at, commands_hash, bound_ts, updated_ts) "
                "VALUES (1, ?, ?, NULL, 0, ?, ?, ?, ?, NULL, ?)",
                (
                    adapter,
                    bot_username,
                    bind_nonce,
                    expires_ts,
                    welcomed_at,
                    commands_hash,
                    now,
                ),
            )

    def complete_bind(self, chat_id: int, update_offset: int, nonce: str) -> bool:
        """Bind the authorized chat: set chat_id, clear the nonce (single-use), stamp bound_ts, and
        advance the cursor past the /start — all one transaction, so a crash never leaves a half-
        bind. Raises if no row was armed (arm_bind must precede)."""
        now = _now()
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE channel SET chat_id = ?, bind_nonce = NULL, "
                "bind_nonce_expires_ts = NULL, bound_ts = ?, "
                "update_offset = ?, updated_ts = ? "
                "WHERE id = 1 AND bind_nonce = ? AND bind_nonce_expires_ts > ?",
                (chat_id, now, update_offset, now, nonce, now),
            )
            if cur.rowcount:
                return True
            if conn.execute("SELECT 1 FROM channel WHERE id = 1").fetchone() is None:
                raise StoreError("no channel row to bind — arm_bind must run first")
            return False

    def clear_bind_nonce(self, nonce: str) -> None:
        """Retire `nonce` if it is still the armed one, leaving the cursor where it is:
        the updates already acked while awaiting the bind stay acked. Drops the row to `off`, so a
        fresh `connect` is what revives it. Guarded on the nonce because the callers decide to
        retire from a row they read earlier — a seller re-running connect in between re-arms the
        row, and an unconditional clear would wipe the fresh nonce."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE channel SET bind_nonce = NULL, bind_nonce_expires_ts = NULL, "
                "updated_ts = ? WHERE id = 1 AND bind_nonce = ?",
                (_now(), nonce),
            )

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
            _stamp_welcomed_in_txn(conn)

    def queue_welcome_notices(self, entries: list) -> None:
        """Queue the bind greeting's messages FIFO and stamp welcomed_at, one transaction —
        greeted-and-queued commit together, so a crash can neither greet twice nor stamp a
        greeting that was never queued. `entries` is [(text, controls | None), ...]."""
        with self._db.transaction() as conn:
            for text, controls in entries:
                _insert_notice(conn, text, controls=controls)
            _stamp_welcomed_in_txn(conn)

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
        like "yes, do that" has the prior turn to resolve against.

        Inbound rows carry their media paths, not just their text. A photo's path would otherwise
        be reachable only by the single pass that claimed its row, so a listing flow spanning more
        than one pass — research here, confirm there — would lose the photo it was sent.
        """
        inbox_rows = self._db.query(
            "SELECT text, kind, media_paths, received_ts FROM channel_inbox "
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
                {
                    "direction": "in",
                    "kind": r["kind"],
                    "text": text,
                    "media_paths": json.loads(r["media_paths"]) if r["media_paths"] else [],
                    "ts": r["received_ts"],
                }
            )
        for r in notice_rows:
            entries.append(
                {
                    "direction": "out",
                    "kind": "notice",
                    "text": r["text"],
                    "media_paths": [],
                    "ts": r["created_ts"],
                }
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

    def has_notice_with_ref(self, ref: str) -> bool:
        """Whether any notice — queued or delivered — was ever queued under `ref`. The durable
        once-guard for proactive pushes (retention never prunes notices)."""
        return bool(self._db.query("SELECT 1 FROM notices WHERE ref = ? LIMIT 1", (ref,)))
