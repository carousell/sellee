"""An account with earlier posts may be offered "copy from another posting" first; the driver only
ever starts a new posting, and presses nothing when it cannot tell which control does that."""

from __future__ import annotations

import pytest

from sellee.browser import craigslist_publisher, publisher
from sellee.browser.markets import craigslist


class _CopyStep:
    def __init__(self, answer):
        self.answer = answer
        self.clicks: list = []
        self.paced = 0

    def evaluate(self, function: str, **_):
        assert function == craigslist.NEW_POSTING_JS
        return self.answer

    def click(self, target: str, element: str) -> None:
        self.clicks.append(target)

    def pace(self, url: str) -> None:
        self.paced += 1


def _step(page) -> None:
    craigslist_publisher._copyfromanother(page, {}, {}, (), {}, lambda _s: None)


def test_the_copy_step_starts_a_new_posting_by_its_radio() -> None:
    page = _CopyStep({"chosen": True, "radio": True, "options": ["copy", "new posting"]})

    _step(page)

    assert page.clicks == [craigslist.CHOICE, craigslist.CONTINUE] and page.paced == 1


def test_the_copy_step_starts_a_new_posting_by_its_button() -> None:
    page = _CopyStep({"chosen": True, "radio": False, "options": ["start a new posting"]})

    _step(page)

    assert page.clicks == [craigslist.CHOICE] and page.paced == 1


def test_a_copy_step_with_no_clear_new_posting_choice_presses_nothing() -> None:
    page = _CopyStep({"chosen": False, "options": ["copy posting #1", "copy posting #2"]})

    with pytest.raises(publisher.PublishNotAttempted, match="copy posting #2"):
        _step(page)

    assert page.clicks == [] and page.paced == 0


def test_the_driver_knows_the_copy_step() -> None:
    assert craigslist_publisher._STEPS["copyfromanother"] is craigslist_publisher._copyfromanother
