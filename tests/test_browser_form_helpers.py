"""The form mechanics a market may borrow, held one at a time.

Each helper is opt-in and knows nothing about any market's sequence, so what is held here is only
its own promise: real input rather than an assigned value, a dropdown option matched by the name it
shows, a price with nothing between its digits, and a read-back that refuses a form which did not
take what it was given.
"""

from __future__ import annotations

import pytest

from sellee.browser.client import BrowserToolError
from sellee.browser.markets import forms
from sellee.browser.markets.publishing import PublishNotAttempted


class StubClient:
    """A page recording what was done to it, answering `evaluate` from a script."""

    def __init__(self, *, answer=None, fail_on=()):
        self.answer = answer
        self.fail_on = set(fail_on)
        self.calls: list = []
        self.evaluated: list = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if arguments.get("target") in self.fail_on:
            raise BrowserToolError("the control went away")
        return ""

    def evaluate(self, function, **kwargs):
        self.evaluated.append(function)
        return self.answer


def _never_pauses(_seconds):
    return None


# --- typed input ----------------------------------------------------------------------------------


def test_a_field_is_typed_into_rather_than_assigned() -> None:
    """A value set from script leaves React holding the old one, and publishes an empty listing."""
    client = StubClient()

    forms.type_text(client, "[data-x='title']", "title", "White Study Desk")

    name, arguments = client.calls[0]
    assert name == "browser_type"
    assert arguments["text"] == "White Study Desk"
    assert arguments["submit"] is False


def test_a_field_that_will_not_take_text_is_a_retryable_refusal() -> None:
    client = StubClient(fail_on=["[data-x='title']"])

    with pytest.raises(PublishNotAttempted) as caught:
        forms.type_text(client, "[data-x='title']", "title", "White Study Desk")

    assert caught.value.retryable
    assert "title" in str(caught.value)


@pytest.mark.parametrize(
    "price,expected",
    [(1299.0, "1299"), (65, "65"), (0.0, "0"), (12.5, "12.5"), (None, ""), ("", "")],
)
def test_a_price_is_typed_with_nothing_between_its_digits(price, expected) -> None:
    """ "1,299" has been read as 1 by more than one marketplace form."""
    assert forms.bare_price(price) == expected


# --- dropdowns ------------------------------------------------------------------------------------


def test_a_dropdown_is_opened_and_the_named_option_clicked() -> None:
    client = StubClient(answer={"chosen": "Used - Good", "options": ["New", "Used - Good"]})

    forms.pick_option_by_name(
        client,
        market="fb",
        step="condition",
        target="[data-x='condition']",
        option_target="[data-x='option']",
        options_js="() => ({})",
        wanted="Used - Good",
        pause=_never_pauses,
    )

    assert [arguments["target"] for _name, arguments in client.calls] == [
        "[data-x='condition']",
        "[data-x='option']",
    ]


def test_a_dropdown_that_offers_nothing_we_asked_for_refuses_and_says_what_it_offers() -> None:
    """Carrying on would press Publish against a form that rejects it."""
    client = StubClient(answer={"chosen": None, "options": ["New", "Refurbished"]})

    with pytest.raises(PublishNotAttempted) as caught:
        forms.pick_option_by_name(
            client,
            market="fb",
            step="condition",
            target="[data-x='condition']",
            option_target="[data-x='option']",
            options_js="() => ({})",
            wanted="Used - Good",
            pause=_never_pauses,
        )

    assert "Refurbished" in str(caught.value)
    assert not caught.value.retryable, "the form does not offer it now and will not on a retry"


def test_a_dropdown_that_will_not_open_is_a_retryable_refusal() -> None:
    client = StubClient(fail_on=["[data-x='condition']"])

    with pytest.raises(PublishNotAttempted) as caught:
        forms.pick_option_by_name(
            client,
            market="fb",
            step="condition",
            target="[data-x='condition']",
            option_target="[data-x='option']",
            options_js="() => ({})",
            wanted="Used - Good",
            pause=_never_pauses,
        )

    assert caught.value.retryable


# --- the read-back --------------------------------------------------------------------------------


_ITEM = {"title": "White Study Desk", "list_price": 65.0}


def test_a_form_holding_what_the_item_said_passes_the_read_back() -> None:
    """The price is compared on digits, so a field free to dress "65" as "$65" still agrees."""
    client = StubClient(answer={"title": "White Study Desk", "price": "$65"})

    forms.check_readback(client, "() => ({})", _ITEM)


@pytest.mark.parametrize(
    "seen",
    [
        {"title": "White Study Des", "price": "$65"},
        {"title": "White Study Desk", "price": "$6"},
    ],
)
def test_a_form_that_changed_what_we_gave_it_refuses(seen) -> None:
    """A field that silently truncated becomes a live listing the seller has to find and fix."""
    client = StubClient(answer=seen)

    with pytest.raises(PublishNotAttempted):
        forms.check_readback(client, "() => ({})", _ITEM)


def test_a_form_that_shows_no_price_at_all_is_left_to_the_forms_own_refusal() -> None:
    """Nothing to compare is not a mismatch; the market's required-fields check owns that case."""
    client = StubClient(answer={"title": "White Study Desk", "price": ""})

    forms.check_readback(client, "() => ({})", _ITEM)
