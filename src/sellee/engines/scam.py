"""Deterministic scam detection for inbound marketplace text — pure, no network, no LLM.

Catches the classic seller-side play (a buyer who offers to "arrange delivery" then sends a link
"to receive the money" — a phishing page) three ways: an off-platform link (not a known
marketplace host), a signature hit (link host / text / a multi-signal playbook against the merged
registry+bank view), or a combo (an agentive delivery phrase plus a receive-money CTA, or a
delivery phrase together with any off-platform link, forces a `scam` verdict).

The engine is pure: the caller passes the current text, the recent inbound window, the marketplace
allowlist, the merged signatures, and the config-derived checkout base. Signatures are DATA
compared here, never executed. `normalize` / `make_id` are the canonical keying shared with the
store so a (kind, value) means the same thing on both sides.
"""

from __future__ import annotations

import hashlib

from sellee.engines import hosts

# Scoring weights (named, not magic).
SCORE_OFFPLATFORM_LINK = 2
SCORE_BANK_HIT = 3
SCORE_DELIVERY_PHRASE = 1
SCORE_RECEIVE_MONEY_PHRASE = 2
VERDICT_SCAM_AT = 3
VERDICT_SUSPICIOUS_AT = 2

# Agentive, first-person delivery-arrangement phrasing a scammer uses ("I'll arrange the courier").
# Deliberately excludes an honest buyer's question ("can you arrange delivery?") — that scores 0.
DELIVERY_PHRASES = (
    "i'll arrange the delivery",
    "i'll arrange delivery",
    "i'll arrange the courier",
    "ill arrange the delivery",
    "ill arrange delivery",
    "i will arrange the delivery",
    "i will arrange delivery",
    "i will arrange the courier",
    "i can arrange the delivery",
    "i can arrange delivery",
    "i can arrange the courier",
    "i've arranged",
    "ive arranged",
    "i have arranged",
    "i'll send my courier",
    "i will send a courier",
    "i'll book the courier",
    "my courier",
    "the courier will",
    "courier will contact",
    "courier will collect",
    "courier will pick",
    "delivery agent will",
    "my delivery agent",
    "i've paid the courier",
    "ive paid the courier",
    "i have paid the courier",
    "paid for the courier",
    "i've already paid the delivery",
    "delivery company will",
)

# Link-to-receive-money / phishing call-to-action phrasing.
RECEIVE_MONEY_PHRASES = (
    "receive the money",
    "receive the payment",
    "receive the funds",
    "receive your payment",
    "receive your money",
    "to receive the",
    "claim your payment",
    "claim the payment",
    "claim your money",
    "confirm your card",
    "confirm your bank",
    "confirm your account",
    "verify your account",
    "verify your card",
    "verify your bank",
    "verify your identity",
    "release the funds",
    "release the payment",
    "release your payment",
    "click the link to",
    "click this link to",
    "click the link below",
    "click on the link",
    "complete the payment on",
    "confirm the delivery on",
    "accept the payment",
    "accept the money",
    "sign in to receive",
    "log in to receive",
    "enter your card",
    "enter your bank details",
    "update your payment",
    "for the money to be released",
)

_ACTIVE_STATUSES = frozenset({"observed", "confirmed"})
_BORN_CONFIRMED = frozenset({"seller_confirm", "seller"})


def normalize(kind: str, value: str) -> str:
    """Canonical comparable form: a domain becomes its bare lowercase host, a pattern its
    whitespace-collapsed lowercase text. The one keying both the store and the merged view use."""
    v = (value or "").strip().lower()
    if kind == "domain":
        host = hosts.host_of(v)
        return hosts.strip_www(host or v)
    return " ".join(v.split())


def make_id(kind: str, value: str) -> str:
    seed = f"{kind}|{normalize(kind, value)}".encode()
    return "scam-" + hashlib.sha256(seed).hexdigest()[:12]  # an id, not a security hash


def classify_links(text: str, allowlist, checkout_base: str, registry_ok: bool = True) -> list:
    """Extract and classify every link in `text`. When the registry could not be read, nothing is
    treated as a marketplace link (fail toward flagging)."""
    out = []
    for raw in hosts.extract_links(text):
        host = hosts.host_of(raw)
        checkout = hosts.is_checkout_link(raw, checkout_base)
        marketplace = registry_ok and hosts.host_is_marketplace(host, allowlist)
        out.append(
            {
                "url": raw,
                "host": host,
                "defanged": hosts.defang(raw),
                "marketplace_match": marketplace,
                "checkout_link": checkout,
            }
        )
    return out


def match_signatures(links: list, lowered_text: str, signatures: list) -> list:
    """Ids of merged signatures matching a link host, the text, or a playbook threshold."""
    link_hosts = {lk["host"] for lk in links if lk.get("host")}
    hits: list = []
    for sig in signatures:
        kind = sig.get("kind")
        value = str(sig.get("value") or "").lower().strip()
        if kind == "domain":
            if value and any(h == value or h.endswith("." + value) for h in link_hosts):
                hits.append(sig.get("id"))
        elif kind in ("message_pattern", "url_pattern"):
            if value and value in lowered_text:
                hits.append(sig.get("id"))
        elif kind == "playbook":
            phrases = [str(s).lower() for s in (sig.get("signals") or [])]
            need = sig.get("min_signals") or 1
            if phrases and sum(1 for ph in phrases if ph and ph in lowered_text) >= need:
                hits.append(sig.get("id"))
    return hits


def scan(
    text: str,
    *,
    history_text: str = "",
    allowlist,
    signatures: list,
    checkout_base: str,
    registry_ok: bool = True,
) -> dict:
    """Score one inbound message. `history_text` is the recent inbound window so a play split
    across messages ("arrange delivery" … then a link) still fires. Returns the verdict dict."""
    window_text = (history_text + "\n" + (text or "")).lower()
    current_links = classify_links(text or "", allowlist, checkout_base, registry_ok)
    window_links = classify_links(window_text, allowlist, checkout_base, registry_ok)

    hits = match_signatures(window_links, window_text, signatures)
    has_offplatform = any(
        (not lk["marketplace_match"]) and (not lk["checkout_link"]) for lk in window_links
    )
    has_delivery = any(p in window_text for p in DELIVERY_PHRASES)
    has_receive_money = any(p in window_text for p in RECEIVE_MONEY_PHRASES)

    signals: list = []
    score = 0
    if has_offplatform:
        score += SCORE_OFFPLATFORM_LINK
        signals.append("offplatform_link")
    if hits:
        score += SCORE_BANK_HIT
        signals.append("bank_hit")
    if has_delivery:
        score += SCORE_DELIVERY_PHRASE
        signals.append("delivery_arrangement_phrase")
    if has_receive_money:
        score += SCORE_RECEIVE_MONEY_PHRASE
        signals.append("receive_money_phrase")

    force_scam = has_delivery and has_offplatform
    if score >= VERDICT_SCAM_AT or force_scam:
        verdict = "scam"
    elif score >= VERDICT_SUSPICIOUS_AT:
        verdict = "suspicious"
    else:
        verdict = "clean"

    return {
        "verdict": verdict,
        "score": score,
        "signals": signals,
        "links": current_links,
        "bank_hits": hits,
        "registry_ok": registry_ok,
    }


def merge_signatures(registry: list, bank: list) -> list:
    """The deterministic match set the scan consumes: registry ∪ active local bank, registry wins
    on a (kind, value) tie, minus anything the seller dismissed on this install."""
    dismissed = {
        (r.get("kind"), normalize(r.get("kind", ""), r.get("value", "")))
        for r in bank
        if r.get("status") == "dismissed"
    }
    merged: dict = {}
    for sig in registry:
        kind = sig.get("kind")
        key = (kind, normalize(kind, sig.get("value", "")))
        if key in dismissed:
            continue
        merged[key] = {
            "id": sig.get("id"),
            "kind": kind,
            "value": normalize(kind, sig.get("value", "")),
            "severity": sig.get("severity", "medium"),
            "source": "registry",
            "play": sig.get("play"),
            "signals": sig.get("signals"),
            "min_signals": sig.get("min_signals"),
        }
    for row in bank:
        if row.get("status") not in _ACTIVE_STATUSES:
            continue
        kind = row.get("kind")
        key = (kind, normalize(kind, row.get("value", "")))
        if key in dismissed or key in merged:
            continue  # registry wins; dismissed suppressed
        merged[key] = {
            "id": row.get("id"),
            "kind": kind,
            "value": normalize(kind, row.get("value", "")),
            "severity": row.get("severity", "medium"),
            "source": "local",
            "play": row.get("play"),
            "signals": None,
            "min_signals": None,
        }
    return list(merged.values())


def born_confirmed(detected_by: str) -> bool:
    """Registry-sourced and seller-confirmed signatures are born confirmed; a detector sighting is
    born observed."""
    return detected_by in _BORN_CONFIRMED or detected_by.startswith("registry:")
