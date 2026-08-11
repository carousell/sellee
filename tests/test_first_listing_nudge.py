"""The first-listing nudge lane: one proactive nudge, ever — bound a day, welcomed, nothing
listed, not skipped. Every guard derives from durable rows (the notice's own ref is the
once-guard), so a daemon restart can never re-fire it.
"""

from __future__ import annotations

from selly_agent.channel import outbound


def _bind_and_welcome(store) -> None:
    store.arm_bind("selly_bot", "n1")
    store.complete_bind(555, update_offset=1)
    store.stamp_welcomed()


def _a_day_later(store) -> float:
    return store.get_channel()["bound_ts"] + outbound.FIRST_LISTING_NUDGE_AGE_SEC + 1


def test_nudge_fires_once_and_is_holdable(store) -> None:
    _bind_and_welcome(store)
    outbound.first_listing_nudge(store=store, now=_a_day_later(store))
    (nudge,) = store.list_queued_notices()
    assert nudge["text"] == outbound.FIRST_LISTING_NUDGE_TEXT
    assert nudge["ref"] == outbound.FIRST_LISTING_NUDGE_REF
    assert nudge["holdable"] is True  # a proactive push waits out quiet hours

    # once, ever: the delivered row still bars a refire — no process-local state involved
    store.mark_notice_delivered(nudge["id"], "channel")
    outbound.first_listing_nudge(store=store, now=_a_day_later(store) + 999_999)
    assert store.count_queued_notices() == 0


def test_nudge_waits_a_full_day_after_bind(store) -> None:
    _bind_and_welcome(store)
    too_soon = store.get_channel()["bound_ts"] + outbound.FIRST_LISTING_NUDGE_AGE_SEC - 10
    outbound.first_listing_nudge(store=store, now=too_soon)
    assert store.count_queued_notices() == 0


def test_nudge_skips_a_seller_who_already_listed(store) -> None:
    _bind_and_welcome(store)
    store.create_item(title="Lamp", list_price=80.0, currency="SGD")
    outbound.first_listing_nudge(store=store, now=_a_day_later(store))
    assert store.count_queued_notices() == 0


def test_nudge_skips_when_unbound(store) -> None:
    outbound.first_listing_nudge(store=store, now=1e12)
    assert store.count_queued_notices() == 0


def test_nudge_skips_before_the_welcome(store) -> None:
    # bound but never greeted (the welcome queue failed): greet first, nudge never
    store.arm_bind("selly_bot", "n1")
    store.complete_bind(555, update_offset=1)
    outbound.first_listing_nudge(store=store, now=_a_day_later(store))
    assert store.count_queued_notices() == 0


def test_nudge_skips_while_paused(store) -> None:
    _bind_and_welcome(store)
    store.set_paused(True, source="test")
    outbound.first_listing_nudge(store=store, now=_a_day_later(store))
    assert store.count_queued_notices() == 0


def test_nudge_respects_the_skip_button(store) -> None:
    _bind_and_welcome(store)
    store.set_meta("first_listing_cta_skipped_ts", "123.0")
    outbound.first_listing_nudge(store=store, now=_a_day_later(store))
    assert store.count_queued_notices() == 0
