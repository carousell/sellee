"""Craigslist's questions to the seller and its unverified posts, as the US live run found them:
the area answer is a tap, a question is asked once across restarts, and a post that may exist is
never driven again."""

from __future__ import annotations

import time
from types import SimpleNamespace

from sellee import crosslist
from sellee.channel import fastpaths
from sellee.tools.seller import BasicsError

_AREAS = ["city of san francisco", "south bay area", "north bay / marin"]


def _tap_area(store, bus, label: str) -> tuple:
    event = {
        "kind": "action",
        "text": fastpaths.CB_CL_AREA,
        "payload": {"ref": label, "choice": fastpaths.CB_CL_AREA},
    }
    return fastpaths.handle_fast_path(store, bus, event)


def test_each_area_is_a_button_that_stores_its_label(store, bus) -> None:
    store.set_seller_config_section("basics", {"region": "US", "zip": "94103"})
    controls = fastpaths.area_controls(_AREAS)

    assert [label for label, _ in controls] == _AREAS
    label, token = controls[2]
    reply, _ = _tap_area(store, bus, token.split(":", 1)[0])

    assert reply == fastpaths.AREA_ACK.format(area="north bay / marin")
    assert store.get_seller_config_section("basics") == {
        "region": "US",
        "zip": "94103",
        "craigslist_area": "north bay / marin",
    }


def test_an_area_tap_is_stored_only_through_validate_basics(store, bus, monkeypatch) -> None:
    """The validator every basics write shares decides what a tap may store."""
    store.set_seller_config_section("basics", {"region": "US", "zip": "94103"})

    def refuse(basics):
        raise BasicsError("refused")

    monkeypatch.setattr(fastpaths, "validate_basics", refuse)
    _tap_area(store, bus, "north bay / marin")

    assert store.get_seller_config_section("basics") == {"region": "US", "zip": "94103"}


def test_an_area_too_long_for_a_button_is_left_to_be_typed() -> None:
    long = "x" * 60

    assert fastpaths.area_controls([long, "peninsula"]) == [
        ("peninsula", f"peninsula:{fastpaths.CB_CL_AREA}")
    ]


def test_a_seller_question_is_not_asked_again_after_a_restart(store) -> None:
    # Live: a daemon restart forgot it had asked, and the area question came twice.
    def deps():
        return SimpleNamespace(store=store, notified={})

    for _ in range(2):  # each a fresh daemon, with its in-memory record gone
        crosslist._notify_once(deps(), "craigslist:craigslist_area:", "Which area?", durable=True)

    assert [n["text"] for n in store.list_queued_notices()] == ["Which area?"]


def test_a_post_that_may_exist_is_never_driven_again(store) -> None:
    store.record_driven_publish(
        "item_1", "craigslist", status="error", origin="crosslist", unverified=True
    )
    a_day_later = time.time() + 86_400

    spent = crosslist._shots_spent(store.publish_pass_index(), a_day_later)

    assert spent == {("item_1", "craigslist"): True}


def test_an_ordinary_failure_is_still_tried_again_later(store) -> None:
    store.record_driven_publish("item_1", "craigslist", status="error", origin="crosslist")
    a_day_later = time.time() + 86_400

    assert crosslist._shots_spent(store.publish_pass_index(), a_day_later) == {}
