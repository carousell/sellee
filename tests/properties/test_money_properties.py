"""Property tests for the one money conversion, at the seam every price passes through.

The example tests in tests/test_money.py pin four inputs. These pin the contract that has to
hold for *every* input, which is what the callers actually rely on: they wrap the call in
`except ValueError` and nothing else, so any other exception type escapes uncaught.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from sellee.money import to_price_cents

# Anything a tool call or a config file could hand this function, well-formed or not. Note that
# st.text() also generates numeric strings, which is how it reached the hole recorded below.
anything = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(),
    st.text(),
    st.decimals(),
    st.lists(st.integers()),
    st.dictionaries(st.text(), st.integers()),
)

# Past the decimal context precision the conversion escapes as InvalidOperation. That hole is
# pinned by test_an_extreme_magnitude_is_rejected_as_value_error, so it is excluded here.
_LIMIT = Decimal(10**20)


def _within_supported_magnitude(value: object) -> bool:
    try:
        dollars = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return True
    return not dollars.is_finite() or abs(dollars) < _LIMIT


@given(anything)
def test_it_returns_an_int_or_raises_value_error_and_nothing_else(value: object) -> None:
    """The total contract. Every caller catches ValueError only, so another exception type
    reaching them is an outage, not a rejected price."""
    assume(_within_supported_magnitude(value))
    try:
        result = to_price_cents(value)
    except ValueError:
        return
    assert isinstance(result, int)
    assert result >= 0


@given(st.decimals(min_value=Decimal("0.01"), max_value=Decimal("10000000"), places=2))
def test_a_two_decimal_price_converts_exactly(dollars: Decimal) -> None:
    """At two decimal places there is nothing to round, so the answer is exact. This is the
    property that would catch a float being introduced into the conversion."""
    assert to_price_cents(dollars) == int(dollars * 100)


@given(
    st.decimals(min_value=Decimal("0.01"), max_value=Decimal("10000000"), places=2),
    st.decimals(min_value=Decimal("0.01"), max_value=Decimal("10000000"), places=2),
)
def test_a_higher_price_never_costs_fewer_cents(a: Decimal, b: Decimal) -> None:
    """Monotonicity. A pricing rule that can invert under rounding is a pricing bug."""
    assume(a <= b)
    assert to_price_cents(a) <= to_price_cents(b)


@pytest.mark.xfail(
    strict=True,
    reason="Known hole: a positive sub-cent price converts to 0 cents, i.e. a free item. "
    "Remove this marker when to_price_cents rejects or floors it deliberately.",
)
@given(st.decimals(min_value=Decimal("0.000001"), max_value=Decimal("0.004"), places=6))
def test_a_positive_price_never_becomes_free(dollars: Decimal) -> None:
    assert to_price_cents(dollars) > 0


@pytest.mark.xfail(
    strict=True,
    reason="Known hole: past the decimal context precision the quantize escapes as "
    "InvalidOperation, the exact failure mode test_money.py already guards for inf/NaN. "
    "Remove this marker when an out-of-range magnitude is rejected as ValueError.",
)
@given(st.decimals(min_value=Decimal("1E+27"), max_value=Decimal("1E+40"), allow_nan=False))
def test_an_extreme_magnitude_is_rejected_as_value_error(dollars: Decimal) -> None:
    with pytest.raises(ValueError):
        to_price_cents(dollars)
