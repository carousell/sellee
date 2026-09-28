"""Editing a live listing by driving its form: what it refuses before saving, how it reads the
listing back afterwards, and that a pre-filled box is emptied before the new value goes in.

The stub form keeps real field state — clicking, select-all, Backspace and per-character typing all
act on it — so "the form received 120" means the box ends up reading 120, not 150120.
"""

from __future__ import annotations

import threading

import pytest

from sellee.browser import editor
from sellee.browser import markets as market_adapters
from sellee.browser.client import BrowserClient, BrowserToolError

_ADAPTER = market_adapters.FACEBOOK
_URL = "https://www.facebook.com/marketplace/item/123/"
_ITEM = {
    "id": "item_1",
    "title": "Dyson V8",
    "list_price": 120.0,
    "description": "Comes with the original box.\nLight scuffs.",
}


class _NoJitter:
    def uniform(self, low: float, high: float) -> float:
        return 0.0


class StubEditForm:
    def __init__(
        self,
        *,
        entry=True,
        marked=("title", "price", "description", "save"),
        values=None,
        after_save=None,
        save_enabled=True,
        boost_on=False,
        fail_on=None,
        wall="",
        clears=True,
    ):
        self._lock = threading.RLock()
        self._sleep = lambda _s: None
        self._rng = _NoJitter()
        self.entry = entry
        self.marked = list(marked)
        # What the marketplace has stored, and what the open form's boxes hold. Opening the form
        # loads the first into the second; Save stores the second — or `after_save`, standing in
        # for a marketplace that did not keep the change.
        self.persisted = dict(
            values or {"title": "Dyson V8", "price": "150", "description": "Works."}
        )
        self.values = dict(self.persisted)
        self.after_save = after_save
        self.save_enabled = save_enabled
        self.boost_on = boost_on
        self.fail_on = fail_on or {}
        self.wall = wall
        self.clears = clears
        self.focused = None
        self.selected = False
        self.saved = None
        self.actions: list = []

    class _Exclusive:
        def __init__(self, client):
            self.client = client

        def __enter__(self):
            return self.client

        def __exit__(self, *exc):
            return False

    def exclusive(self):
        return self._Exclusive(self)

    def navigate_visible(self, url):
        self.actions.append(("navigate", url))

    type_humanly = BrowserClient.type_humanly
    _click_into = BrowserClient._click_into

    def call_tool(self, name, arguments):
        target = arguments.get("target", "")
        step = target.split("'")[1] if "'" in target else None
        self.actions.append((name, step or arguments.get("key")))
        if name in self.fail_on or (step and step in self.fail_on):
            raise BrowserToolError(self.fail_on.get(name) or self.fail_on.get(step))
        if name == "browser_click":
            self.focused = step
            self.selected = False
            if step == "entry":
                self.values = dict(self.persisted)
            if step == "save":
                self.saved = dict(self.values)
                kept = self.after_save if self.after_save is not None else self.values
                self.persisted = dict(kept)
            if step == "boost":
                self.boost_on = False
        elif name == "browser_press_key":
            if arguments["key"] == "ControlOrMeta+a":
                self.selected = True
            elif arguments["key"] == "Backspace" and self.selected and self.clears:
                self.values[self.focused] = ""
                self.selected = False
            elif arguments["key"] == "Shift+Enter":
                self.values[self.focused] = self.values.get(self.focused, "") + "\n"
        elif name == "browser_type":
            self.values[step] = self.values.get(step, "") + arguments["text"]
        return ""

    def evaluate(self, function, **kwargs):
        if function == _ADAPTER.block_wall_js:
            return self.wall
        if function == _ADAPTER.edit_entry_js:
            return {"found": self.entry}
        if function == _ADAPTER.edit_fields_js:
            return {
                "marked": list(self.marked),
                "missing": [],
                "save_enabled": self.save_enabled,
                "boost_on": self.boost_on,
            }
        if function == _ADAPTER.edit_readback_js:
            return dict(self.values)
        raise AssertionError(f"unexpected artifact: {function[:60]}")


def _revise(form, changed=("list_price",), item=None):
    return editor.revise(
        form,
        _ADAPTER,
        item or _ITEM,
        listing_url=_URL,
        changed=list(changed),
        sleep=lambda _s: None,
    )


# --- the happy path -----------------------------------------------------------------------------


def test_a_price_edit_empties_the_box_then_types_and_is_checked_by_reopening_the_form() -> None:
    form = StubEditForm(values={"title": "Dyson V8", "price": "SGD150", "description": "Works."})
    outcome = _revise(form)
    assert form.saved["price"] == "120"  # not "SGD150120"
    assert outcome.verified is True
    assert outcome.accepted["price"] == "120"
    # reached from the recorded URL by the page's own Edit control, and checked the same way
    assert form.actions[0] == ("navigate", _URL)
    navigations = [a for a in form.actions if a[0] == "navigate"]
    assert navigations == [("navigate", _URL), ("navigate", _URL)]
    assert form.actions.count(("browser_click", "entry")) == 2
    assert form.actions.count(("browser_click", "save")) == 1


def test_only_the_named_fields_are_touched() -> None:
    form = StubEditForm()
    _revise(form, changed=("list_price",))
    assert form.saved["title"] == "Dyson V8" and form.saved["description"] == "Works."
    typed = {step for name, step in form.actions if name == "browser_type"}
    assert typed == {"price"}


def test_a_multi_line_description_is_verified_whole() -> None:
    form = StubEditForm()
    outcome = _revise(form, changed=("description",))
    assert form.saved["description"] == _ITEM["description"]
    assert outcome.verified is True


# --- refusals before anything is saved ----------------------------------------------------------


def test_a_box_that_did_not_empty_is_caught_before_saving() -> None:
    form = StubEditForm(clears=False)
    with pytest.raises(editor.ReviseNotAttempted, match="price"):
        _revise(form)
    assert form.saved is None


def test_a_page_with_no_edit_control_is_not_this_seller_s_listing() -> None:
    form = StubEditForm(entry=False)
    with pytest.raises(editor.ReviseNotAttempted) as info:
        _revise(form)
    assert info.value.retryable is True
    assert form.saved is None


def test_a_form_missing_a_field_it_needs_changes_nothing() -> None:
    form = StubEditForm(marked=("title", "description", "save"))
    with pytest.raises(editor.ReviseNotAttempted, match="price"):
        _revise(form)
    assert not any(name == "browser_type" for name, _ in form.actions)


def test_a_wall_stops_it_before_anything_is_typed() -> None:
    form = StubEditForm(wall="checkpoint")
    with pytest.raises(editor.ReviseNotAttempted, match="refusing the account") as info:
        _revise(form)
    assert info.value.retryable is True
    assert not any(name == "browser_type" for name, _ in form.actions)


def test_a_field_the_driver_cannot_change_is_not_attempted_at_all() -> None:
    form = StubEditForm()
    with pytest.raises(editor.ReviseNotAttempted, match="photos") as info:
        _revise(form, changed=("list_price", "photos"))
    assert info.value.retryable is False
    assert form.actions == []


def test_a_disabled_save_is_not_pressed() -> None:
    form = StubEditForm(save_enabled=False)
    with pytest.raises(editor.ReviseNotAttempted):
        _revise(form)
    assert form.saved is None


def test_a_paid_boost_is_switched_off_before_saving() -> None:
    form = StubEditForm(boost_on=True)
    _revise(form)
    assert ("browser_click", "boost") in form.actions
    assert form.saved is not None


# --- after the save -----------------------------------------------------------------------------


def test_a_form_still_holding_the_old_price_is_unverified_and_names_it() -> None:
    form = StubEditForm(
        after_save={"title": "Dyson V8", "price": "SGD150", "description": "Works."}
    )
    outcome = _revise(form)
    assert outcome.verified is False
    assert outcome.mismatched == ("list_price",)
    assert "price" in outcome.reason


def test_an_unreadable_form_after_saving_is_unverified_not_failed() -> None:
    form = StubEditForm(after_save={})
    with pytest.raises(editor.ReviseUnverified):
        _revise(form)


def test_a_browser_error_on_the_save_is_unverified() -> None:
    form = StubEditForm(fail_on={"save": "detached"})
    with pytest.raises(editor.ReviseUnverified):
        _revise(form)


# --- capability ---------------------------------------------------------------------------------


def test_capability_is_read_off_the_adapter() -> None:
    assert editor.can_edit("fb")
    assert editor.can_edit_fields("fb", ["list_price", "title", "description"])
    assert not editor.can_edit_fields("fb", ["photos"])
    assert not editor.can_edit("carousell")  # a recipe market, not a driven one
    assert not editor.can_edit("carousell-ai")


@pytest.mark.parametrize(
    "shown,price,matches",
    [
        ("115.00", 115, True),  # Carousell's JSON-LD, read live
        ("SGD115", 115, True),  # Facebook's edit form, read live
        ("S$1,299", 1299, True),
        ("120", 120.0, True),
        ("SGD150", 120, False),
        ("115.50", 115, False),
        ("", 120, True),  # no number shown says nothing either way
    ],
)
def test_prices_are_compared_as_numbers(shown, price, matches) -> None:
    from sellee.browser import formfill

    assert formfill.price_matches(shown, price) is matches


def test_a_price_box_that_did_not_load_after_saving_is_a_mismatch() -> None:
    # Review: a missing read used to count as a match, so an edit whose price box never loaded
    # after Save was reported as landed.
    form = StubEditForm(after_save={"title": "Dyson V8", "price": "", "description": "Works."})
    outcome = _revise(form)
    assert outcome.verified is False
    assert outcome.mismatched == ("list_price",)


def test_a_description_can_be_cleared() -> None:
    # Review: an empty value was skipped, so clearing a description left the old one in place.
    form = StubEditForm()
    outcome = _revise(form, changed=("description",), item=dict(_ITEM, description=""))
    assert form.saved["description"] == ""
    assert outcome.verified is True
    assert ("browser_press_key", "Backspace") in form.actions
