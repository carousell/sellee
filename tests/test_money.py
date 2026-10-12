"""to_price_cents is the one place a dollar amount becomes the integer cents everything downstream
(the checkout rail, the publish tool) trusts. Every caller wraps the call in `except ValueError` —
so the contract that matters isn't just "rejects bad input," it's "always rejects it as ValueError."
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from sellee.money import to_price_cents

_PROPERTY = settings(max_examples=200, deadline=None)


def test_a_typical_price_rounds_half_up_to_cents() -> None:
    assert to_price_cents("19.995") == 2000
    assert to_price_cents(19.99) == 1999
    assert to_price_cents(20) == 2000


def test_non_positive_input_is_rejected() -> None:
    with pytest.raises(ValueError):
        to_price_cents(0)
    with pytest.raises(ValueError):
        to_price_cents(-5)


def test_a_price_that_rounds_to_zero_cents_is_rejected() -> None:
    with pytest.raises(ValueError):
        to_price_cents("0.001")
    with pytest.raises(ValueError):
        to_price_cents("0.004")
    assert to_price_cents("0.005") == 1


def test_non_numeric_input_is_rejected() -> None:
    with pytest.raises(ValueError):
        to_price_cents("not a number")
    with pytest.raises(ValueError):
        to_price_cents(None)


def test_infinity_and_nan_are_rejected_as_value_error_not_decimal_error() -> None:
    """Both slip past the `<= 0` positivity check (it's False/raises for neither), so without an
    explicit finiteness check they reach the quantize() call and blow up with
    decimal.InvalidOperation — a type none of this function's callers catch."""
    with pytest.raises(ValueError):
        to_price_cents(float("inf"))
    with pytest.raises(ValueError):
        to_price_cents(float("-inf"))
    with pytest.raises(ValueError):
        to_price_cents(float("nan"))
    with pytest.raises(ValueError):
        to_price_cents("Infinity")
    with pytest.raises(ValueError):
        to_price_cents("NaN")


# Anything a tool call or a config file could hand this function, well-formed or not. st.text()
# also generates numeric strings.
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


@_PROPERTY
@given(anything)
def test_it_returns_an_int_or_raises_value_error_and_nothing_else(value: object) -> None:
    """The total contract. Every caller catches ValueError only, so another exception type
    reaching them is an outage, not a rejected price."""
    try:
        result = to_price_cents(value)
    except ValueError:
        return
    assert isinstance(result, int)
    assert result > 0


@_PROPERTY
@given(st.decimals(min_value=Decimal("0.01"), max_value=Decimal("10000000"), places=2))
def test_a_two_decimal_price_converts_exactly(dollars: Decimal) -> None:
    """At two decimal places there is nothing to round, so the answer is exact. This is the
    property that would catch a float being introduced into the conversion."""
    assert to_price_cents(dollars) == int(dollars * 100)


@_PROPERTY
@given(
    st.decimals(min_value=Decimal("0.01"), max_value=Decimal("10000000"), places=2),
    st.decimals(min_value=Decimal("0.01"), max_value=Decimal("10000000"), places=2),
)
def test_a_higher_price_never_costs_fewer_cents(a: Decimal, b: Decimal) -> None:
    """Monotonicity. A pricing rule that can invert under rounding is a pricing bug."""
    assume(a <= b)
    assert to_price_cents(a) <= to_price_cents(b)


@_PROPERTY
@given(st.decimals(min_value=Decimal("0.000001"), max_value=Decimal("0.004999"), places=6))
def test_a_positive_price_under_half_a_cent_is_rejected_not_made_free(dollars: Decimal) -> None:
    """It passes the positivity check but rounds to 0 cents, which would publish or check out as
    a free item."""
    with pytest.raises(ValueError):
        to_price_cents(dollars)


@_PROPERTY
@given(st.integers(min_value=1, max_value=999), st.integers(min_value=26, max_value=2_000_000))
def test_a_magnitude_past_the_decimal_range_is_rejected_as_value_error(
    digits: int, exponent: int
) -> None:
    """From 1E+26 dollars the cents outgrow the decimal context's 28 digits and the quantize
    raises InvalidOperation; past its exponent range the multiply raises Overflow. Neither is a
    ValueError, so either would escape every caller, as infinity and NaN once did."""
    with pytest.raises(ValueError):
        to_price_cents(Decimal(f"{digits}E+{exponent}"))
