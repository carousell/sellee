"""The scam signature bank."""

from __future__ import annotations

from sellee.db import Database
from sellee.engines import scam as scam_engine
from sellee.store.helpers import StoreError, _load_scam_registry, _now


class ScamMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

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
