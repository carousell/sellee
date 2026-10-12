"""Scheduler: due computation, no self-overlap, backoff arithmetic, per-attempt events."""

from __future__ import annotations

from sellee.scheduler import (
    BACKOFF_CAP_SEC,
    Scheduler,
    Task,
)


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


def _kinds(bus):
    return [e.kind for e in bus.store.read()]


def test_run_once_runs_due_task_and_ledgers_it(bus) -> None:
    calls = []
    sched = Scheduler(bus, tick_interval_sec=1.0)
    sched.register(Task("t", interval_sec=10.0, func=lambda: calls.append(1)))

    sched.run_once()
    assert calls == [1]
    assert _kinds(bus) == ["task.start", "task.ok"]


def test_on_tick_hook_runs_each_tick(bus) -> None:
    ticks = []
    sched = Scheduler(bus, tick_interval_sec=1.0, on_tick=lambda: ticks.append(1))
    sched.register(Task("t", interval_sec=10.0, func=lambda: None))
    sched.run_once()
    assert ticks == [1]


def test_task_does_not_overlap_itself(bus) -> None:
    clock = FakeClock()
    sched = Scheduler(bus, tick_interval_sec=1.0, clock=clock)
    sched.register(Task("t", interval_sec=10.0, func=lambda: None))

    first = sched._claim_due(clock.now)
    assert [t.name for t in first] == ["t"]  # claimed, now marked running

    second = sched._claim_due(clock.now)
    assert second == []  # still running -> not re-claimed


def test_backoff_is_capped_exponential(bus) -> None:
    sched = Scheduler(bus, tick_interval_sec=1.0)
    assert sched._backoff_delay(1) == 5.0
    assert sched._backoff_delay(2) == 10.0
    assert sched._backoff_delay(3) == 20.0
    assert sched._backoff_delay(50) == BACKOFF_CAP_SEC


def test_failure_arms_backoff_and_success_resets(bus) -> None:
    clock = FakeClock()
    should_fail = {"v": True}

    def func():
        if should_fail["v"]:
            raise RuntimeError("boom")

    sched = Scheduler(bus, tick_interval_sec=1.0, clock=clock)
    sched.register(Task("t", interval_sec=10.0, func=func))
    task = sched._reg.tasks["t"]

    sched._reg.state["t"].running = True
    sched._run_task(task)
    st = sched._reg.state["t"]
    assert st.consecutive_failures == 1
    assert st.backoff_until == clock.now + 5.0
    assert not st.running
    assert _kinds(bus) == ["task.start", "task.error", "task.backoff"]

    sched._reg.state["t"].running = True
    sched._run_task(task)
    assert sched._reg.state["t"].consecutive_failures == 2
    assert sched._reg.state["t"].backoff_until == clock.now + 10.0

    should_fail["v"] = False
    sched._reg.state["t"].running = True
    sched._run_task(task)
    st = sched._reg.state["t"]
    assert st.consecutive_failures == 0
    assert st.backoff_until == 0.0
    assert _kinds(bus)[-1] == "task.ok"


def test_backoff_gate_blocks_claim_until_it_passes(bus) -> None:
    clock = FakeClock()
    sched = Scheduler(bus, tick_interval_sec=1.0, clock=clock)
    sched.register(Task("t", interval_sec=1.0, func=lambda: None))
    st = sched._reg.state["t"]
    st.backoff_until = clock.now + 30.0

    assert sched._claim_due(clock.now) == []
    clock.advance(31.0)
    assert [t.name for t in sched._claim_due(clock.now)] == ["t"]


# --- jitter: the browser lanes must not arrive on a grid ------------------------------------------
#
# A lane that fires every 300.000s forever is the cheapest thing a marketplace can detect about an
# account: an inter-arrival histogram with no variance. These pin that the jitter is real, that it
# is bounded, and — the regression that matters — that it did not quietly cost `--once` its
# guarantee that one tick exercises every lane.


class FakeRandom:
    """A uniform() that walks a script, so a jittered schedule is exact rather than approximate."""

    def __init__(self, *values):
        self.values = list(values)
        self.calls: list = []

    def uniform(self, low: float, high: float) -> float:
        self.calls.append((low, high))
        return self.values.pop(0) if self.values else high


def test_an_unjittered_task_keeps_its_exact_interval(bus) -> None:
    clock = FakeClock()
    sched = Scheduler(bus, tick_interval_sec=1.0, clock=clock)
    sched.register(Task("t", interval_sec=10.0, func=lambda: None))

    sched._claim_due(clock.now)
    assert sched._reg.state["t"].next_due == clock.now + 10.0


def test_a_jittered_task_spreads_its_next_due(bus) -> None:
    clock = FakeClock()
    rng = FakeRandom(0.8)
    # stagger off, so the only draw is the recurring one this test is about.
    sched = Scheduler(bus, tick_interval_sec=1.0, clock=clock, rng=rng, stagger=False)
    sched.register(Task("t", interval_sec=100.0, func=lambda: None, jitter=0.3))

    sched._claim_due(clock.now)

    assert rng.calls == [(0.7, 1.3)]
    assert sched._reg.state["t"].next_due == clock.now + 80.0


def test_jitter_stays_inside_its_band(bus) -> None:
    clock = FakeClock()
    sched = Scheduler(bus, tick_interval_sec=1.0, clock=clock)
    sched.register(Task("t", interval_sec=300.0, func=lambda: None, jitter=0.3))

    for _ in range(200):
        st = sched._reg.state["t"]
        st.running = False
        st.next_due = clock.now
        sched._claim_due(clock.now)
        assert 210.0 <= st.next_due - clock.now <= 390.0


def test_backoff_arithmetic_is_untouched_by_jitter(bus) -> None:
    sched = Scheduler(bus, tick_interval_sec=1.0, rng=FakeRandom(1.3))
    assert sched._backoff_delay(1) == 5.0
    assert sched._backoff_delay(2) == 10.0
    assert sched._backoff_delay(99) == BACKOFF_CAP_SEC


# --- the first tick -------------------------------------------------------------------------------


def test_staggering_spreads_the_first_tick_of_a_jittered_lane(bus) -> None:
    """The boot burst is its own signal: without this every browser lane fires at once, every
    start, and the marketplace sees the same opening volley each time."""
    clock = FakeClock()
    sched = Scheduler(bus, tick_interval_sec=1.0, clock=clock, rng=FakeRandom(42.0))
    sched.register(Task("t", interval_sec=300.0, func=lambda: None, jitter=0.3))

    assert sched._reg.state["t"].next_due == clock.now + 42.0
    assert sched._claim_due(clock.now) == []


def test_an_unjittered_lane_is_still_due_on_the_first_tick(bus) -> None:
    clock = FakeClock()
    sched = Scheduler(bus, tick_interval_sec=1.0, clock=clock)
    sched.register(Task("t", interval_sec=300.0, func=lambda: None))

    assert [t.name for t in sched._claim_due(clock.now)] == ["t"]


def test_without_stagger_every_lane_is_due_on_the_first_tick(bus) -> None:
    """What `--once` rests on: one tick exercises every lane, jittered or not. A lane that is built
    but never scheduled is the failure that smoke run exists to catch, and a staggered first due
    would hide exactly that."""
    clock = FakeClock()
    sched = Scheduler(bus, tick_interval_sec=1.0, clock=clock, stagger=False)
    sched.register(Task("jittered", interval_sec=300.0, func=lambda: None, jitter=0.3))
    sched.register(Task("plain", interval_sec=300.0, func=lambda: None))

    assert sorted(t.name for t in sched._claim_due(clock.now)) == ["jittered", "plain"]
