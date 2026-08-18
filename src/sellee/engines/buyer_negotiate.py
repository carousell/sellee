"""Buy-side negotiation engine — the mirror of the sell engine, pure over a caller-supplied ledger.

Per-want state machine spanning many sellers (one thing the buyer wants, pursued across several
listings at once): open a thread with an aggressive offer below list (never above the secret max
budget); as the seller replies, climb toward their ask but stay strictly under the max, accepting
the moment their price is within budget; walk away politely (no number) if they stay firm above
budget after the rounds. accept commits one thread and returns the sibling threads to close.

The max budget is confidential (the inverse of the floor): it is read only here and never appears
in any returned dict. The _guard assert is the backstop — no emitted offer ever exceeds the max.
"""

from __future__ import annotations

DEFAULT_OPENING_RATIO = 0.8
DEFAULT_STEP = 5
DEFAULT_MAX_ROUNDS = 2


class AboveMaxError(AssertionError):
    """Defensive backstop: a decision tried to emit an offer above the max budget. Never leaks a
    value."""


def blank_seller(handle: str, listed_price) -> dict:
    return {
        "seller_handle": handle,
        "listed_price": listed_price,
        "our_offers": [],
        "our_highest_offer": 0,
        "seller_lowest_ask": None,
        "rounds_used": 0,
        "last_offer": None,
        "agreed_price": None,
        "status": "negotiating",
    }


def decide_open(listed_price, target, max_budget, opening_ratio) -> dict:
    """The aggressive opening offer: below list, near the target, under the ceiling. If the listing
    is already at/under what we'd open with and within budget, just accept."""
    base = opening_ratio * listed_price
    if target is not None:
        base = min(target, base)
    opening = max(1, min(int(round(base)), max_budget))
    if listed_price <= max_budget and opening >= listed_price:
        return {
            "decision": "accept",
            "accept_price": int(listed_price),
            "message_intent": f"accept:{int(listed_price)}",
        }
    return {
        "decision": "opening_offer",
        "offer_price": opening,
        "message_intent": f"open:{opening}",
    }


def decide_reply(
    seller_price, seller, led, thread_id, target, max_budget, step, max_rounds
) -> dict:
    """The seller stated `seller_price`. Returns a dict; the max budget is never included."""
    if led["state"] == "committed" and led.get("committed_thread") != thread_id:
        return {"decision": "stand_down", "message_intent": "bought_elsewhere"}

    last = seller["last_offer"]
    rounds = seller["rounds_used"]

    if last is not None and seller_price <= last:
        deal = int(min(seller_price, last))
        return {"decision": "accept", "accept_price": deal, "message_intent": f"accept:{deal}"}

    if seller_price <= target:
        return {
            "decision": "accept",
            "accept_price": int(seller_price),
            "message_intent": f"accept:{int(seller_price)}",
        }

    if seller_price <= max_budget:
        if rounds < max_rounds:
            proposed = max(target, seller_price - step)
            if last is not None:
                proposed = max(proposed, last)  # never lower our own offer (monotonic)
            proposed = min(proposed, max_budget)
            if proposed < seller_price:
                return {
                    "decision": "counter",
                    "offer_price": int(proposed),
                    "message_intent": f"counter:{int(proposed)}",
                }
        return {
            "decision": "accept",
            "accept_price": int(seller_price),
            "message_intent": f"accept:{int(seller_price)}",
        }

    if rounds >= max_rounds:
        return {"decision": "walk_away", "message_intent": "walk"}
    base = last if last is not None else 0
    proposed = base + step * (rounds + 1)
    proposed = max(proposed, base)
    proposed = min(proposed, max_budget - 1)  # strictly under the ceiling
    proposed = min(proposed, seller_price - 1)  # still under their ask while haggling
    if last is not None and proposed <= last:
        return {"decision": "hold", "offer_price": int(last), "message_intent": f"hold:{int(last)}"}
    return {
        "decision": "counter",
        "offer_price": int(proposed),
        "message_intent": f"counter:{int(proposed)}",
    }


def guard(res: dict, max_budget) -> dict:
    """Defensive: no emitted price ever exceeds the max budget (mirror of the floor assert)."""
    for key in ("offer_price", "accept_price"):
        if res.get(key) is not None and res[key] > max_budget:
            raise AboveMaxError("offer above max budget — refusing to emit")
    return res
