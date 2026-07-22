"""Sell-side negotiation engine — pure decision functions over a ledger the caller supplies.

Per-item state machine spanning both platforms (one physical item, many buyers):

  * below list  → counter toward list (never below the floor), anti-probing (a counter cap +
    sticky ceiling), lowball deflection, and never accept below another active buyer's standing
    offer (effective_min = max(floor, other_best + 1));
  * at list     → FCFS: the first buyer to commit at list holds it provisionally;
  * above list  → bidding: the highest bid leads but is NEVER auto-accepted — it returns
    needs_seller_confirm so the seller approves it (the Harry-$200 guard);
  * sold        → terminal.

The floor is confidential: it is read only here and never appears in any returned dict. The
below-floor assert is the defensive backstop — the engine refuses to emit a price under the floor
rather than leak one. Knob resolution is a per-item floor override, then config, then defaults,
with a stubbed seam where firmness presets will later sit.
"""

from __future__ import annotations

DEFAULTS = {"max_counters": 2, "min_offer_ratio": 0.6, "lowball_cap": 3}
DEFAULT_STEP = 10


class BelowFloorError(AssertionError):
    """Defensive backstop: a decision tried to emit a price below the floor. Never leaks a value."""


def resolve_knobs(config, floor_record: dict | None = None) -> dict:
    """Per-item floor override → config → defaults. The style/firmness layer will slot between the
    floor override and config later; for now it is a pass-through. Returns step + the three
    anti-probing knobs."""
    floor_record = floor_record or {}
    step = floor_record.get("auto_counter_step")
    max_counters = floor_record.get("auto_counter_rounds")
    return {
        "step": step if step is not None else DEFAULT_STEP,
        "max_counters": (
            max_counters
            if max_counters is not None
            else getattr(config, "negotiation_max_counters", DEFAULTS["max_counters"])
        ),
        "min_offer_ratio": getattr(
            config, "negotiation_min_offer_ratio", DEFAULTS["min_offer_ratio"]
        ),
        "lowball_cap": getattr(config, "negotiation_lowball_cap", DEFAULTS["lowball_cap"]),
    }


def blank_buyer(handle: str) -> dict:
    return {
        "buyer_handle": handle,
        "offers": [],
        "highest_offer": 0,
        "rounds_used": 0,
        "last_counter": None,
        "lowball_count": 0,
        "status": "active",
    }


def record_offer(buyer: dict, offer, held_for_floor: bool = False) -> None:
    """Append the buyer's offer (in place). A pre-floor offer is marked held_for_floor; the
    post-floor re-decide of the SAME amount consumes that hold instead of appending a duplicate."""
    last = buyer["offers"][-1] if buyer["offers"] else None
    if last and last.get("held_for_floor") and last["amount"] == offer:
        if not held_for_floor:
            last.pop("held_for_floor", None)  # the floor arrived; this decision consumes the hold
    else:
        row = {"amount": offer}
        if held_for_floor:
            row["held_for_floor"] = True
        buyer["offers"].append(row)
    buyer["highest_offer"] = max(buyer["highest_offer"], int(offer))


def other_best(buyers: dict, thread_id: str) -> int:
    best = 0
    for tid, b in buyers.items():
        if tid != thread_id and b.get("status") not in ("passed", "lost"):
            best = max(best, b.get("highest_offer", 0))
    return best


def decide_below_list(
    offer, buyer, effective_min, list_price, step, max_counters, min_offer_ratio, lowball_cap
):
    """< list haggling: counter toward list, never below effective_min, capped + sticky. Whole
    dollars (int) — the marketplace convention here."""
    last, rounds = buyer["last_counter"], buyer["rounds_used"]
    if last is not None and offer >= last and offer >= effective_min:
        return "accept_fcfs", int(offer), False, f"accept:{int(offer)}"
    if rounds >= max_counters:
        hold = max(last if last is not None else list_price, effective_min)
        return "hold_firm", int(hold), True, f"hold:{int(hold)}"
    if offer < min_offer_ratio * list_price:
        if buyer["lowball_count"] + 1 >= lowball_cap:
            return "hold_firm", int(last if last is not None else list_price), True, "disengage"
        return "deflect_lowball", None, False, "decline_lowball"
    target = list_price - step * (rounds + 1)
    ceiling = last if last is not None else list_price
    counter = max(effective_min, min(target, ceiling))
    if counter <= offer:
        return "accept_fcfs", int(offer), False, f"accept:{int(offer)}"
    return "counter", int(counter), False, f"counter:{int(counter)}"


def decide(offer, thread_id, buyer, led, floor, list_price, step, knobs):
    """Top-level routing. Returns a dict — the floor value is never included."""
    if led["state"] == "sold":
        return {"decision": "sold", "needs_seller_confirm": False, "message_intent": "item_sold"}

    fr = led.get("front_runner")
    top_bid = fr["amount"] if (fr and fr.get("kind") == "bid") else list_price

    if offer > list_price:
        current_top = top_bid if led["is_bidding"] else list_price
        if offer > current_top:
            return {
                "decision": "bid_lead",
                "leading_amount": int(offer),
                "bar_to_beat": int(current_top),
                "needs_seller_confirm": True,
                "message_intent": f"bid_lead:{int(offer)}",
            }
        return {
            "decision": "bid_outbid",
            "bar_to_beat": int(current_top),
            "needs_seller_confirm": False,
            "message_intent": f"beat_bar:{int(current_top)}",
        }

    if offer == list_price:
        if led["is_bidding"]:
            return {
                "decision": "bid_outbid",
                "bar_to_beat": int(top_bid),
                "needs_seller_confirm": False,
                "message_intent": f"beat_bar:{int(top_bid)}",
            }
        if fr is None or fr["thread_id"] == thread_id:
            return {
                "decision": "accept_fcfs",
                "accept_price": int(list_price),
                "needs_seller_confirm": False,
                "message_intent": f"accept:{int(list_price)}",
            }
        return {
            "decision": "fcfs_taken",
            "needs_seller_confirm": False,
            "message_intent": "pending_fcfs",
        }

    if led["is_bidding"]:
        return {
            "decision": "bid_outbid",
            "bar_to_beat": int(top_bid),
            "needs_seller_confirm": False,
            "message_intent": f"beat_bar:{int(top_bid)}",
        }
    if fr is not None and fr["thread_id"] != thread_id:
        return {
            "decision": "fcfs_taken",
            "needs_seller_confirm": False,
            "message_intent": "pending_fcfs",
        }
    ob = other_best(led["buyers"], thread_id)
    effective_min = max(floor, (ob + 1) if ob else floor)
    dec, counter, hold, intent = decide_below_list(
        offer,
        buyer,
        effective_min,
        list_price,
        step,
        knobs["max_counters"],
        knobs["min_offer_ratio"],
        knobs["lowball_cap"],
    )
    if counter is not None and counter < floor:
        raise BelowFloorError("price below floor — refusing to emit")
    res = {
        "decision": dec,
        "counter_price": counter,
        "hold_firm": hold,
        "needs_seller_confirm": False,
        "message_intent": intent,
    }
    if dec == "accept_fcfs":
        res["accept_price"] = int(counter)
    return res
