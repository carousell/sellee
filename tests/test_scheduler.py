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
