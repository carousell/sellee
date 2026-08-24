"""Seller config, the meta KV, the settings change ledger, and pause control."""

from __future__ import annotations

import json

from sellee.db import Database
from sellee.store import (
    PendingChangeRecord,
    _decide_pending_in_txn,
    _insert_notice,
    _json,
    _new_id,
    _now,
    _pending_change_from_row,
    _supersede_pending_in_txn,
    _upsert_setting_in_txn,
)


class SettingsMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

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

    def seller_region(self) -> str | None:
        """Which regional site of a marketplace this seller posts on, or None if not recorded.

        Every URL the agent composes and every one it verifies is pinned to this, so it is read from
        here rather than accepted from a caller — and normalized here for the same reason. The
        registry keys its regional sites by code ("SG"), and an exact-match lookup on "sg" resolves
        to no site at all, which reads downstream as "this marketplace isn't available to you".
        """
        region = (self.get_seller_config_section("basics") or {}).get("region")
        return str(region).strip().upper() or None if region else None

    def get_seller_config_public(self) -> dict:
        """Every section except the private origin address — the buyer-safe view a read tool may
        return."""
        rows = self._db.query("SELECT section, value FROM seller_config")
        return {
            r["section"]: json.loads(r["value"])
            for r in rows
            if r["section"] not in self._SELLER_CONFIG_PRIVATE
        }

    # --- meta: the generic durable KV (one key per writer, documented at the writer) ------------

    def get_meta(self, key: str) -> str | None:
        rows = self._db.query("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

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
