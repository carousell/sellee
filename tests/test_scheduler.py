"""Scheduler: due computation, no self-overlap, backoff arithmetic, per-attempt events."""

from __future__ import annotations

from selly_agent.scheduler import (
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


def _run_once_claimed(sched, name: str) -> None:
    """One full attempt: claim (which commits the static next_due), then run."""
    sched._claim_due(sched._clock())
    sched._run_task(sched._reg.tasks[name])


def test_dynamic_cadence_pulls_the_next_run_forward(bus) -> None:
    clock = FakeClock()
    sched = Scheduler(bus, tick_interval_sec=1.0, clock=clock)
    sched.register(Task("t", interval_sec=300.0, func=lambda: None, next_interval=lambda: 60.0))

    _run_once_claimed(sched, "t")
    assert sched._reg.state["t"].next_due == clock.now + 60.0


def test_dynamic_cadence_never_delays_past_the_static_interval(bus) -> None:
    clock = FakeClock()
    sched = Scheduler(bus, tick_interval_sec=1.0, clock=clock)
    sched.register(Task("t", interval_sec=300.0, func=lambda: None, next_interval=lambda: 9000.0))

    _run_once_claimed(sched, "t")
    assert sched._reg.state["t"].next_due == clock.now + 300.0


def test_dynamic_cadence_is_re_evaluated_each_run_so_it_decays(bus) -> None:
    clock = FakeClock()
    interval = {"v": 60.0}
    sched = Scheduler(bus, tick_interval_sec=1.0, clock=clock)
    sched.register(
        Task("t", interval_sec=300.0, func=lambda: None, next_interval=lambda: interval["v"])
    )

    _run_once_claimed(sched, "t")
    assert sched._reg.state["t"].next_due == clock.now + 60.0

    # the conversation goes cold: the very next run falls back to the registered interval
    interval["v"] = 300.0
    clock.advance(60.0)
    _run_once_claimed(sched, "t")
    assert sched._reg.state["t"].next_due == clock.now + 300.0


def test_a_raising_cadence_callable_leaves_the_static_schedule_and_no_backoff(bus) -> None:
    clock = FakeClock()

    def boom() -> float:
        raise RuntimeError("no store")

    sched = Scheduler(bus, tick_interval_sec=1.0, clock=clock)
    sched.register(Task("t", interval_sec=300.0, func=lambda: None, next_interval=boom))

    _run_once_claimed(sched, "t")
    st = sched._reg.state["t"]
    assert st.next_due == clock.now + 300.0
    assert st.consecutive_failures == 0
    assert st.backoff_until == 0.0
    assert _kinds(bus) == ["task.start", "task.ok"]


def test_a_failed_run_backs_off_and_ignores_the_dynamic_cadence(bus) -> None:
    clock = FakeClock()

    def func():
        raise RuntimeError("boom")

    sched = Scheduler(bus, tick_interval_sec=1.0, clock=clock)
    sched.register(Task("t", interval_sec=300.0, func=func, next_interval=lambda: 1.0))

    _run_once_claimed(sched, "t")
    st = sched._reg.state["t"]
    assert st.next_due == clock.now + 300.0  # not pulled in to +1.0
    assert st.backoff_until == clock.now + 5.0


def test_a_task_without_a_dynamic_cadence_is_untouched(bus) -> None:
    clock = FakeClock()
    sched = Scheduler(bus, tick_interval_sec=1.0, clock=clock)
    sched.register(Task("t", interval_sec=300.0, func=lambda: None))

    _run_once_claimed(sched, "t")
    assert sched._reg.state["t"].next_due == clock.now + 300.0
