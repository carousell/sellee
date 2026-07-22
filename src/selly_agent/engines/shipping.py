"""Deterministic P2P delivery-fee computation — the buyer's total, computed in code, never
improvised by the model.

buyer_total = list_price + delivery_fee + size_surcharge, from the seller's zone table. First
matching zone wins, in the order the seller listed them (an area match, a distance band, or the
__else__ catch-all); a destination outside every serviceable zone returns covered=false with null
fees. The size surcharge keys off the item's size bucket (default `small`).

The seller's exact origin address is deliberately NOT an input here — the destination arrives as
area text and/or a distance, so this engine never even loads the address it must never emit.

Caveat carried from the legacy port: the zone machinery is PM-built and not prod-tested (internal
testing runs in SG where a flat rate covers the country — one zone / default fee). Ported as-is.
"""

from __future__ import annotations

CATCH_ALL = "__else__"


def resolve_zone(zones: list, dest_area: str, dest_km) -> dict | None:
    """First matching zone wins: an exact area (or the __else__ catch-all), then a distance band,
    evaluated in the seller's listed order. Returns the zone dict or None."""
    area_key = (dest_area or "").strip().lower()
    for zone in zones:
        match = zone.get("match", {})
        areas = [a.lower() for a in match.get("areas", [])]
        if area_key and area_key in areas:
            return zone
        if CATCH_ALL in areas:
            return zone
        if "max_km" in match and dest_km is not None and dest_km <= match["max_km"]:
            return zone
    return None


def compute(
    zones: list,
    size_surcharge: dict,
    price,
    size_bucket: str,
    currency: str,
    dest_area: str,
    dest_km,
) -> dict:
    """Pure fee computation. An unserviceable destination (no zone, or a zone with a null fee)
    returns covered=false with null delivery_fee/total_fee/buyer_total."""
    zone = resolve_zone(zones, dest_area, dest_km)
    surcharge = size_surcharge.get(size_bucket, 0)

    if zone is None or zone.get("fee") is None:
        return {
            "zone": zone["zone"] if zone else None,
            "delivery_fee": None,
            "size_surcharge": surcharge,
            "total_fee": None,
            "price": price,
            "buyer_total": None,
            "currency": currency,
            "covered": False,
        }

    delivery_fee = zone["fee"]
    total_fee = delivery_fee + surcharge
    return {
        "zone": zone["zone"],
        "delivery_fee": delivery_fee,
        "size_surcharge": surcharge,
        "total_fee": total_fee,
        "price": price,
        "buyer_total": price + total_fee,
        "currency": currency,
        "covered": True,
    }
