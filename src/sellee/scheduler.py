"""Scheduler — one loop thread computing due tasks and submitting them to a small executor.

A task never runs concurrently with itself (an in-flight guard). Every attempt is ledgered:
task.start, then task.ok or task.error, so a lane that is alive-but-failing-every-attempt is
detectable from the event log, not hidden behind a ticking heartbeat. Consecutive failures arm
a capped exponential backoff (reset on success) so a fast-failing task backs off instead of
retrying hot. Scheduling arithmetic uses the monotonic clock; only the event store stamps wall
time.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from sellee.events import EventBus

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
    # How far either side of `interval_sec` this task's next run may wander, as a fraction: 0.3
    # means 70%–130% of the interval. Zero — the default — keeps the exact interval, which is what
    # a latency path wants.
    #
    # Set it on a lane whose work is visible to someone outside this machine. A marketplace read
    # arriving every 300.000 seconds forever is the cheapest thing an account-integrity model can
    # notice: it needs no fingerprinting, only an inter-arrival histogram with no variance. Leave
    # it at zero for `pass_lane` and `market_connect`, whose two-second interval is how quickly a
    # seller's own tap is answered.
    jitter: float = 0.0


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
        rng: object = None,
        stagger: bool = True,
    ):
        self._bus = bus
        self._tick_interval = tick_interval_sec
        self._on_tick = on_tick
        self._clock = clock
        self._rng = rng if rng is not None else random
        # Whether a jittered task's *first* run is spread too. Separate from the jitter itself
        # because the two answer different questions: jitter shapes the steady state, staggering
        # shapes the boot. `--once` turns it off — that smoke run rests on one tick exercising
        # every lane, and a lane that is built but never scheduled is precisely what it exists to
        # catch, so a staggered first due would hide the bug rather than find it.
        self._stagger = stagger
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._reg = _Registry()
        self._lock = threading.Lock()
        # Shared with the daemon so a signal that arrives during startup is already recorded
        # by the time the loop begins.
        self._stop = stop_event if stop_event is not None else threading.Event()

    def register(self, task: Task) -> None:
        if task.interval_sec < self._tick_interval:
            # Due-ness is only evaluated at tick boundaries, so an interval under the tick is a
            # request the loop cannot serve: the lane runs once per tick and no faster. Said out
            # loud because it is otherwise invisible — the typing pulse declared 4.0s, ran at
            # ~5.14s, and carried a comment claiming it kept a 5s indicator alive. That one is gone
            # (it is a thread now, see channel/presence.py); the lanes that still warn here are all
            # latency targets, where a tick's rounding costs nothing and a deadline is not being
            # missed. If a lane ever appears here that is holding something *open*, it belongs on a
            # thread too.
            log.warning(
                "task %s asks for %.1fs but the scheduler ticks every %.1fs — it will run at the "
                "tick, not its interval",
                task.name,
                task.interval_sec,
                self._tick_interval,
            )
        with self._lock:
            self._reg.tasks[task.name] = task
            # Due immediately on the first tick, so a fresh start exercises each lane — except a
            # jittered one under staggering, which starts somewhere inside its first interval so
            # the browser lanes do not all fire together on every boot.
            self._reg.state[task.name] = _TaskState(
                next_due=self._clock() + self._first_delay(task)
            )

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

    def _first_delay(self, task: Task) -> float:
        """How long after registration a task first becomes due."""
        if not task.jitter or not self._stagger:
            return 0.0
        return self._rng.uniform(0.0, task.interval_sec * task.jitter)

    def _next_interval(self, task: Task) -> float:
        """This task's next gap. Exact unless the task asked to wander."""
        if not task.jitter:
            return task.interval_sec
        return task.interval_sec * self._rng.uniform(1.0 - task.jitter, 1.0 + task.jitter)

    def _claim_due(self, now: float) -> list[Task]:
        due: list[Task] = []
        with self._lock:
            for name, task in self._reg.tasks.items():
                st = self._reg.state[name]
                if st.running or now < st.next_due or now < st.backoff_until:
                    continue
                st.running = True
                st.next_due = now + self._next_interval(task)
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
            with self._lock:
                st = self._reg.state.get(task.name)
                if st is None:  # deregistered mid-run
                    return
                st.consecutive_failures = 0
                st.backoff_until = 0.0
                st.running = False
            self._bus.publish("task.ok", {"task": task.name})

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
