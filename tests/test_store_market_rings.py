"""The notifications a marketplace has rung with, and whether the visit each asked for happened.

Chrome replays every notification it recorded on every look, so what makes a ring heard once is this
table: keyed on the notification's own identity, a replay adds nothing and a restart neither loses
a ring nor answers it twice.
"""

from __future__ import annotations

_FB = "fb"


def test_a_ring_is_heard_once_however_often_it_is_replayed(store) -> None:
    rings = [("mid.$a|1000.000", "message", 1000.0)]

    assert store.record_rings(_FB, rings, due_ts=1090.0, now=1001.0) == 1
    assert store.record_rings(_FB, rings, due_ts=1200.0, now=1030.0) == 0


def test_a_message_ring_is_owed_a_visit_once_it_is_due(store) -> None:
    store.record_rings(_FB, [("mid.$a|1000.000", "message", 1000.0)], due_ts=1090.0, now=1001.0)

    assert not store.ring_owed(_FB, now=1050.0)
    assert store.ring_owed(_FB, now=1090.0)


def test_a_ring_that_is_not_a_message_asks_for_nothing(store) -> None:
    store.record_rings(_FB, [("like.1|1000.000", "other", 1000.0)], due_ts=1000.0, now=1001.0)

    assert not store.ring_owed(_FB, now=5000.0)


def test_a_visit_answers_what_was_heard_before_it_began_and_no_more(store) -> None:
    """A ring that arrives while the visit is under way was not read by it."""
    store.record_rings(_FB, [("mid.$a|1000.000", "message", 1000.0)], due_ts=1000.0, now=1001.0)
    store.record_rings(_FB, [("mid.$b|1100.000", "message", 1100.0)], due_ts=1100.0, now=1101.0)

    assert store.answer_rings(_FB, heard_before=1050.0, now=1200.0) == 1

    assert store.ring_owed(_FB, now=1200.0)
    assert store.answer_rings(_FB, heard_before=1200.0, now=1300.0) == 1
    assert not store.ring_owed(_FB, now=1300.0)


def test_rings_on_one_market_are_nothing_to_another(store) -> None:
    store.record_rings(_FB, [("mid.$a|1000.000", "message", 1000.0)], due_ts=1000.0, now=1001.0)

    assert not store.ring_owed("carousell", now=2000.0)


def test_a_ring_heard_already_answered_asks_for_nothing(store) -> None:
    """What the doorbell hears the first time it listens can be days old. It is kept, as evidence
    the doorbell works, and asks for no visit."""
    store.record_rings(
        _FB, [("mid.$old|10.000", "message", 10.0)], due_ts=1000.0, now=1001.0, answered=True
    )

    assert not store.ring_owed(_FB, now=5000.0)
    assert store.last_ring_ts(_FB) == 10.0


def test_the_last_ring_is_whenever_the_market_last_rang_at_all(store) -> None:
    assert store.last_ring_ts(_FB) is None
    store.record_rings(
        _FB,
        [("like.1|500.000", "other", 500.0), ("mid.$a|900.000", "message", 900.0)],
        due_ts=1000.0,
        now=1001.0,
    )

    assert store.last_ring_ts(_FB) == 900.0


def test_answered_rings_are_let_go_after_a_month(store) -> None:
    day = 86400.0
    store.record_rings(_FB, [("mid.$a|0.000", "message", 0.0)], due_ts=0.0, now=1.0)
    store.answer_rings(_FB, heard_before=2.0, now=2.0)

    store.record_rings(_FB, [("mid.$b|x", "message", 40 * day)], due_ts=40 * day, now=40 * day)

    assert store.last_ring_ts(_FB) == 40 * day
    assert store.ring_count(_FB) == 1
