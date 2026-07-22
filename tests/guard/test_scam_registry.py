"""The shipped scam registry is a supply-chain surface (it ships to every install and is read
into scam-detection context), so its schema + safety rules are enforced here in CI: a bad edit
fails the suite, not a tester's runtime. Ported from the legacy scam_registry_check — entries must
be DATA, never instructions or navigable links.
"""

from __future__ import annotations

import json
import re

from selly_agent import marketplaces
from selly_agent.engines import scam

VALID_KINDS = frozenset({"domain", "url_pattern", "message_pattern", "playbook"})
VALID_SEVERITIES = frozenset({"low", "medium", "high"})
VALID_SOURCES = frozenset({"community", "maintainer"})

MAX_SIGNATURES = 5000
MAX_VALUE_LEN = 200
MAX_DESC_LEN = 200
MAX_SIGNAL_LEN = 80
MAX_SIGNALS = 8

_ID_RE = re.compile(r"^sig-\d{4,}$")
_PLAY_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_INJECTION_SHAPES = ("`", "${", "\n", "\r", "\t")


def _bad_text(text: str):
    if "://" in text:
        return "contains '://' (registry values must never be navigable links)"
    if any(shape in text for shape in _INJECTION_SHAPES) or any(ord(c) < 0x20 for c in text):
        return "contains a newline / control char / backtick / '${' (injection-shaped)"
    return None


def _validate_signals(sig, kind, where, errors):
    signals = sig.get("signals")
    if kind == "playbook":
        if not isinstance(signals, list) or not (1 <= len(signals) <= MAX_SIGNALS):
            errors.append(f"{where}.signals must be a list of 1-{MAX_SIGNALS} for a playbook")
            return
        for s in signals:
            if not isinstance(s, str) or not s.strip() or len(s) > MAX_SIGNAL_LEN:
                errors.append(f"{where}.signals has an invalid entry")
                continue
            bad = _bad_text(s)
            if bad:
                errors.append(f"{where}.signals entry {bad}")
        mn = sig.get("min_signals")
        if not isinstance(mn, int) or not (1 <= mn <= len(signals)):
            errors.append(f"{where}.min_signals must be an int in 1..{len(signals)}")
    elif signals is not None:
        errors.append(f"{where}.signals is only valid for a playbook")


def _validate(doc) -> list:
    errors: list = []
    if not isinstance(doc, dict):
        return ["top-level value is not an object"]
    if not isinstance(doc.get("schema_version"), int):
        errors.append("schema_version must be an integer")
    if not isinstance(doc.get("updated"), str) or not doc.get("updated"):
        errors.append("updated must be a non-empty string")

    sigs = doc.get("signatures")
    if not isinstance(sigs, list):
        return errors + ["signatures must be a list"]
    if len(sigs) > MAX_SIGNATURES:
        errors.append(f"too many signatures ({len(sigs)} > {MAX_SIGNATURES})")

    known_markets = {e["id"] for e in marketplaces.all_marketplaces() if e.get("id")}
    seen_ids: set = set()
    seen_keys: set = set()

    for i, sig in enumerate(sigs):
        where = f"signatures[{i}]"
        if not isinstance(sig, dict):
            errors.append(f"{where} is not an object")
            continue
        sid = sig.get("id")
        if not isinstance(sid, str) or not _ID_RE.match(sid):
            errors.append(f"{where}.id must match sig-NNNN")
        elif sid in seen_ids:
            errors.append(f"{where}.id '{sid}' is duplicated")
        else:
            seen_ids.add(sid)

        kind = sig.get("kind")
        if kind not in VALID_KINDS:
            errors.append(f"{where}.kind '{kind}' invalid")

        value = sig.get("value")
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{where}.value must be a non-empty string")
        else:
            if len(value) > MAX_VALUE_LEN:
                errors.append(f"{where}.value too long (>{MAX_VALUE_LEN})")
            bad = _bad_text(value)
            if bad:
                errors.append(f"{where}.value {bad}")
            if kind in VALID_KINDS:
                key = (kind, scam.normalize(kind, value))
                if key in seen_keys:
                    errors.append(f"{where} duplicate (kind, value): {key}")
                else:
                    seen_keys.add(key)

        if sig.get("severity") not in VALID_SEVERITIES:
            errors.append(f"{where}.severity invalid")
        if sig.get("source") not in VALID_SOURCES:
            errors.append(f"{where}.source must be community|maintainer")

        play = sig.get("play")
        if play is not None and (not isinstance(play, str) or not _PLAY_RE.match(play)):
            errors.append(f"{where}.play must be a [a-z0-9_] slug (<=40)")

        desc = sig.get("description", "")
        if not isinstance(desc, str):
            errors.append(f"{where}.description must be a string")
        elif desc:
            if len(desc) > MAX_DESC_LEN:
                errors.append(f"{where}.description too long (>{MAX_DESC_LEN})")
            bad = _bad_text(desc)
            if bad:
                errors.append(f"{where}.description {bad}")

        markets = sig.get("marketplaces")
        if not isinstance(markets, list) or not markets:
            errors.append(f"{where}.marketplaces must be a non-empty list")
        elif known_markets:
            for m in markets:
                if m != "*" and m not in known_markets:
                    errors.append(f"{where}.marketplaces has unknown id '{m}'")

        _validate_signals(sig, kind, where, errors)

    return errors


def test_shipped_scam_registry_is_valid() -> None:
    doc = json.loads(marketplaces.SCAM_REGISTRY_PATH.read_text())
    errors = _validate(doc)
    assert errors == [], "shipped scam_registry.json failed validation:\n" + "\n".join(errors)


def test_validator_catches_a_navigable_link() -> None:
    # a guard on the guard: a poisoned entry with a navigable link must be rejected
    bad = {
        "schema_version": 1,
        "updated": "2026-01-01",
        "signatures": [
            {
                "id": "sig-9999",
                "kind": "message_pattern",
                "value": "visit https://evil.example/steal",
                "severity": "high",
                "source": "community",
                "marketplaces": ["*"],
            }
        ],
    }
    assert any("navigable" in e for e in _validate(bad))
