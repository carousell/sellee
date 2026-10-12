"""How a person's cursor gets to a control, and where on it they click.

A click from Playwright's locator arrives at the exact centre of the element with no movement before
it: the cursor is simply there. A page that records pointer events sees that on every click, which
no person produces. This decides a person's version instead: a point inside the control, near its
middle but not on it; a curved path from wherever the cursor was, in a person's number of steps and
a person's time, easing in and out; and a press held for a moment rather than for nothing.

Pure: positions and a random source in, steps out. The caller moves the mouse and sleeps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# How many moves a path is made of, and how long the whole journey takes. The time grows with the
# distance, as a reach across the screen takes longer than a nudge.
MIN_STEPS = 8
MAX_STEPS = 25
MIN_DURATION_SEC = 0.18
MAX_DURATION_SEC = 0.9
# How far the curve may bow off the straight line, as a fraction of the distance.
MAX_BOW = 0.18
# How long the button is held down, in milliseconds.
PRESS_MS = (45, 130)
# How far from the middle a click lands, as a fraction of the half-size, at one standard deviation.
AIM_SPREAD = 0.3


@dataclass(frozen=True)
class Box:
    """A control's bounding box, in the page's CSS pixels."""

    x: float
    y: float
    width: float
    height: float

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x <= self.x + self.width and self.y <= y <= self.y + self.height


@dataclass(frozen=True)
class Step:
    """One move, and how long to wait before it."""

    x: float
    y: float
    delay_sec: float


def aim(box: Box, rng) -> tuple:
    """Where on the control to click: near the middle, not on it, always inside."""
    half_w, half_h = box.width / 2, box.height / 2
    dx = _clamp(rng.gauss(0.0, AIM_SPREAD), -0.8, 0.8) * half_w
    dy = _clamp(rng.gauss(0.0, AIM_SPREAD), -0.8, 0.8) * half_h
    return (box.x + half_w + dx, box.y + half_h + dy)


def path(start: tuple, end: tuple, rng) -> tuple:
    """The moves from `start` to `end`: a gently bowed curve, eased in and out, ending on `end`."""
    (sx, sy), (ex, ey) = start, end
    distance = math.hypot(ex - sx, ey - sy)
    steps = int(_clamp(MIN_STEPS + distance / 60.0, MIN_STEPS, MAX_STEPS))
    duration = _clamp(0.2 + distance / 1500.0, MIN_DURATION_SEC, MAX_DURATION_SEC)
    duration *= rng.uniform(0.85, 1.15)
    # Two control points either side of the line, pushed off it along its normal.
    nx, ny = (-(ey - sy) / distance, (ex - sx) / distance) if distance else (0.0, 0.0)
    bows = [rng.uniform(-MAX_BOW, MAX_BOW) * distance for _ in range(2)]
    c1 = (sx + (ex - sx) / 3 + nx * bows[0], sy + (ey - sy) / 3 + ny * bows[0])
    c2 = (sx + 2 * (ex - sx) / 3 + nx * bows[1], sy + 2 * (ey - sy) / 3 + ny * bows[1])
    moves = []
    for index in range(1, steps + 1):
        t = _ease(index / steps)
        x, y = _bezier(t, (sx, sy), c1, c2, (ex, ey))
        moves.append(Step(x=x, y=y, delay_sec=duration / steps * rng.uniform(0.7, 1.3)))
    last = moves[-1]
    moves[-1] = Step(x=ex, y=ey, delay_sec=last.delay_sec)
    return tuple(moves)


def press_ms(rng) -> int:
    """How long the button is held down."""
    return int(rng.uniform(*PRESS_MS))


def _ease(t: float) -> float:
    return t * t * (3 - 2 * t)


def _bezier(t: float, p0: tuple, p1: tuple, p2: tuple, p3: tuple) -> tuple:
    u = 1 - t
    x = u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0]
    y = u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1]
    return (x, y)


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)
