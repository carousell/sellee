"""The pointer: how a person's cursor gets to a control and where on it they click.

A click that arrives from nowhere at the exact centre of a button, every time, is a thing a page
can measure. The promises here are that the path ends where the click lands, moves in a person's
number of steps, never goes wildly off course, takes a person's time, and that the click lands
inside the control but not always at its middle.
"""

from __future__ import annotations

import math
import random

from hypothesis import given
from hypothesis import strategies as st

from sellee.engines import pointer

_BOX = pointer.Box(x=400.0, y=600.0, width=120.0, height=36.0)

coords = st.floats(min_value=0, max_value=2000, allow_nan=False)
seeds = st.integers(min_value=0, max_value=2**32)


@given(seeds)
def test_the_click_lands_inside_the_control(seed) -> None:
    x, y = pointer.aim(_BOX, random.Random(seed))

    assert _BOX.x <= x <= _BOX.x + _BOX.width
    assert _BOX.y <= y <= _BOX.y + _BOX.height


def test_the_click_is_not_always_the_middle() -> None:
    points = {pointer.aim(_BOX, random.Random(seed)) for seed in range(50)}

    assert len(points) > 40


@given(coords, coords, coords, coords, seeds)
def test_the_path_ends_where_the_click_lands(sx, sy, ex, ey, seed) -> None:
    steps = pointer.path((sx, sy), (ex, ey), random.Random(seed))

    assert (steps[-1].x, steps[-1].y) == (ex, ey)


@given(coords, coords, coords, coords, seeds)
def test_a_path_takes_a_persons_number_of_steps_and_time(sx, sy, ex, ey, seed) -> None:
    steps = pointer.path((sx, sy), (ex, ey), random.Random(seed))

    assert pointer.MIN_STEPS <= len(steps) <= pointer.MAX_STEPS
    assert all(step.delay_sec > 0 for step in steps)
    total = sum(step.delay_sec for step in steps)
    assert pointer.MIN_DURATION_SEC * 0.5 <= total <= pointer.MAX_DURATION_SEC * 1.5


@given(coords, coords, coords, coords, seeds)
def test_a_path_never_wanders_far_off_the_line(sx, sy, ex, ey, seed) -> None:
    """A curve, not a detour: every point stays within a fraction of the distance, plus a little,
    of the straight line between the ends."""
    steps = pointer.path((sx, sy), (ex, ey), random.Random(seed))
    length = math.hypot(ex - sx, ey - sy)
    slack = pointer.MAX_BOW * length + 2.0

    for step in steps:
        assert _distance_to_segment((step.x, step.y), (sx, sy), (ex, ey)) <= slack + 1e-6


def test_paths_are_curves_that_differ() -> None:
    a = pointer.path((0.0, 0.0), (800.0, 400.0), random.Random(1))
    b = pointer.path((0.0, 0.0), (800.0, 400.0), random.Random(2))

    assert [(s.x, s.y) for s in a] != [(s.x, s.y) for s in b]
    middle = a[len(a) // 2]
    assert _distance_to_segment((middle.x, middle.y), (0.0, 0.0), (800.0, 400.0)) > 1.0


def test_the_press_is_held_for_a_persons_moment() -> None:
    for seed in range(50):
        held = pointer.press_ms(random.Random(seed))
        assert pointer.PRESS_MS[0] <= held <= pointer.PRESS_MS[1]


def test_the_same_randomness_moves_the_same_way() -> None:
    assert pointer.path((5.0, 5.0), (500.0, 300.0), random.Random(9)) == pointer.path(
        (5.0, 5.0), (500.0, 300.0), random.Random(9)
    )


def _distance_to_segment(p, a, b) -> float:
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    squared = dx * dx + dy * dy
    if squared == 0:  # a zero-length segment, or one so short its square underflows
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / squared))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))
