"""Scheduler — one loop thread computing due tasks and submitting them to a small executor.

A task never runs concurrently with itself (an in-flight guard). Every attempt is ledgered:
task.start, then task.ok or task.error, so a lane that is alive-but-failing-every-attempt is
detectable from the event log, not hidden behind a ticking heartbeat. Consecutive failures arm
a capped exponential backoff (reset on success) so a fast-failing task backs off instead of
retrying hot. A task may also carry a dynamic cadence, which can only pull its next run forward
from the registered interval. Scheduling arithmetic uses the monotonic clock; only the event store
stamps wall time.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from selly_agent.events import EventBus

log = logging.getLogger(__name__)

# Small pool; effective concurrency is a handful of I/O-bound tasks. Not hardcoded at call sites.
MAX_WORKERS = 4

BACKOFF_BASE_SEC = 5.0
BACKOFF_CAP_SEC = 300.0

# A loop body (excluding the inter-tick sleep) longer than this is a stall worth a WARN.
LOOP_ITER_BUDGET_SEC = 30.0


@dataclass(frozen=True)
class Task:
    name: str
    interval_sec: float
    func: Callable[[], None]
    # An optional dynamic cadence, consulted after a successful run; it can only bring the next run
    # forward, never push it out. Evaluated after the run, not in the due computation: that way the
    # run that finds new work is the one that speeds the lane up, and off the loop lock.
    next_interval: Callable[[], float] | None = None


@dataclass
class _TaskState:
    next_due: float
    consecutive_failures: int = 0
    backoff_until: float = 0.0
    running: bool = False


@dataclass
class _Registry:
    tasks: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)


class Scheduler:
    def __init__(
        self,
        bus: EventBus,
        *,
        tick_interval_sec: float,
        max_workers: int = MAX_WORKERS,
        on_tick: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        stop_event: threading.Event | None = None,
    ):
        self._bus = bus
        self._tick_interval = tick_interval_sec
        self._on_tick = on_tick
        self._clock = clock
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._reg = _Registry()
        self._lock = threading.Lock()
        # Shared with the daemon so a signal that arrives during startup is already recorded
        # by the time the loop begins.
        self._stop = stop_event if stop_event is not None else threading.Event()

    def register(self, task: Task) -> None:
        with self._lock:
            self._reg.tasks[task.name] = task
            # due immediately on the first tick, so a fresh start exercises each lane
            self._reg.state[task.name] = _TaskState(next_due=self._clock())

    def deregister(self, name: str) -> None:
        """Remove a task so it stops being scheduled — used when a channel provider is torn down,
        so its delivery lanes don't linger as no-ops. Safe to call on a running scheduler and even
        while the task is mid-run (its completion tolerates the missing state)."""
        with self._lock:
            self._reg.tasks.pop(name, None)
            self._reg.state.pop(name, None)

    def request_stop(self) -> None:
        self._stop.set()

    # --- one iteration --------------------------------------------------------------------

    def _backoff_delay(self, consecutive_failures: int) -> float:
        return min(BACKOFF_BASE_SEC * (2 ** (consecutive_failures - 1)), BACKOFF_CAP_SEC)

    def _claim_due(self, now: float) -> list[Task]:
        due: list[Task] = []
        with self._lock:
            for name, task in self._reg.tasks.items():
                st = self._reg.state[name]
                if st.running or now < st.next_due or now < st.backoff_until:
                    continue
                st.running = True
                st.next_due = now + task.interval_sec
                due.append(task)
        return due

    def _run_task(self, task: Task) -> None:
        self._bus.publish("task.start", {"task": task.name})
        try:
            task.func()
        except Exception as exc:
            with self._lock:
                st = self._reg.state.get(task.name)
                if st is None:  # deregistered mid-run — nothing to reschedule
                    return
                st.consecutive_failures += 1
                delay = self._backoff_delay(st.consecutive_failures)
                st.backoff_until = self._clock() + delay
                failures = st.consecutive_failures
                st.running = False
            log.warning("task %s failed (attempt %d): %s", task.name, failures, exc)
            self._bus.publish("task.error", {"task": task.name, "error": repr(exc)})
            self._bus.publish(
                "task.backoff",
                {"task": task.name, "delay_sec": delay, "consecutive_failures": failures},
            )
        else:
            pull_in = self._dynamic_next_due(task)  # before the lock: it may query the store
            with self._lock:
                st = self._reg.state.get(task.name)
                if st is None:  # deregistered mid-run
                    return
                st.consecutive_failures = 0
                st.backoff_until = 0.0
                st.running = False
                if pull_in is not None:
                    st.next_due = min(st.next_due, pull_in)
            self._bus.publish("task.ok", {"task": task.name})

    def _dynamic_next_due(self, task: Task) -> float | None:
        """The earliest a dynamically-paced task may run again, or None to keep the static
        schedule — which is also what a raising callable gets: the task's own work succeeded, and a
        cadence hint is not worth turning that into an error."""
        if task.next_interval is None:
            return None
        try:
            return self._clock() + float(task.next_interval())
        except Exception as exc:
            log.warning("next_interval for task %s failed: %s", task.name, exc)
            return None

    def tick(self) -> list[Future]:
        if self._on_tick is not None:
            self._on_tick()
        now = self._clock()
        return [self._executor.submit(self._run_task, task) for task in self._claim_due(now)]

    # --- drivers --------------------------------------------------------------------------

    def run_once(self) -> None:
        """One tick; block until the tasks it submitted finish (so their events are recorded)."""
        futures = self.tick()
        if futures:
            wait(futures)

    def run(self) -> None:
        """Loop until stop: tick, then sleep one interval (interruptible by request_stop)."""
        while not self._stop.is_set():
            started = self._clock()
            self.tick()
            elapsed = self._clock() - started
            if elapsed > LOOP_ITER_BUDGET_SEC:
                log.warning(
                    "scheduler loop iteration took %.1fs (budget %.0fs)",
                    elapsed,
                    LOOP_ITER_BUDGET_SEC,
                )
            self._stop.wait(self._tick_interval)

    def shutdown(self) -> None:
        """Stop accepting work and drain in-flight tasks (bounded by the tasks themselves)."""
        self._stop.set()
        self._executor.shutdown(wait=True)
