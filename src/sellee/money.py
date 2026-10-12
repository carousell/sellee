"""The one deterministic money rule — dollars to integer cents, in code, never in a prompt.

The LLM never composes a price in cents; a tool converts the item's list price here so the
conversion is auditable and consistent. Half-up rounding matches the legacy engine; a
non-positive, non-finite, or non-numeric input is rejected rather than coerced.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, DecimalException, InvalidOperation


def to_price_cents(list_price: object) -> int:
    """Dollars (int/float/str) -> integer cents, half-up. Rejects non-positive, non-finite
    (Infinity/NaN), non-numeric, or out-of-range input, and a price that rounds to zero cents —
    always as ValueError."""
    try:
        dollars = Decimal(str(list_price))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"list_price is not numeric: {list_price!r}") from exc
    if not dollars.is_finite():
        raise ValueError(f"list_price must be finite, got {dollars}")
    if dollars <= 0:
        raise ValueError(f"list_price must be positive, got {dollars}")
    try:
        cents = (dollars * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise ValueError(f"list_price is out of range, got {dollars}") from exc
    if cents <= 0:
        raise ValueError(f"list_price rounds to zero cents, got {dollars}")
    return int(cents)
