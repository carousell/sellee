"""Hold bazaar's mail stream open and read relay or registration mail when it rings. A ring is a
hint: the reads also run on a first open, after a dropped stream, and every five minutes."""

from __future__ import annotations

import json
import logging
import random as _random
import threading
from collections.abc import Callable, Iterable, Iterator
from typing import Protocol

log = logging.getLogger(__name__)

KINDS = ("relay", "registration")
KEEP_ALIVE_SEC = 20.0
# A stream silent for two keep-alives is treated as dropped.
READ_TIMEOUT_SEC = 2 * KEEP_ALIVE_SEC + 5
FIRST_WAIT_MAX_SEC = 30.0
WAIT_CAP_SEC = 60.0


class StreamMissing(Exception):
    """This bazaar answered without a mail stream; the five-minute reads carry the mail."""


class Stream(Protocol):
    def events(self) -> Iterator[tuple[str, str | None]]: ...

    def close(self) -> None: ...


def parse_events(lines: Iterable[str]) -> Iterator[tuple[str, str | None]]:
    """Server-sent events as (name, kind): ready, and a ring naming a kind sellee reads."""
    event, data = None, ""
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if event == "ready":
                yield ("ready", None)
            elif event == "ring":
                try:
                    kind = json.loads(data).get("kind")
                except (ValueError, AttributeError):
                    kind = None
                if kind in KINDS:
                    yield ("ring", kind)
            event, data = None, ""
        elif line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data += line[len("data:") :].strip()


class SingleFlight:
    """Runs func on one thread at a time; a call while it runs becomes one more run after it."""

    def __init__(self, func: Callable[[], None]):
        self._func = func
        self._lock = threading.Lock()
        self._running = False
        self._again = False

    def run(self) -> None:
        with self._lock:
            if self._running:
                self._again = True
                return
            self._running = True
        while True:
            try:
                self._func()
            except Exception:
                with self._lock:
                    self._running = self._again = False
                raise
            with self._lock:
                if not self._again:
                    self._running = False
                    return
                self._again = False


def reconnect_wait(failures: int, random: Callable[[], float] = _random.random) -> float:
    """Seconds to wait after the nth failure in a row: 1–30 at random, doubling, at most 60, so
    installs dropped together do not all come back at once."""
    first = 1.0 + (FIRST_WAIT_MAX_SEC - 1.0) * random()
    return min(WAIT_CAP_SEC, first * 2 ** (failures - 1))


class MailStream:
    """bazaar's mail stream, read on its own thread: `run` until `shutdown`."""

    def __init__(
        self,
        *,
        open_stream: Callable[[], Stream],
        reads: dict[str, SingleFlight],
        random: Callable[[], float] = _random.random,
    ):
        self._open = open_stream
        self._reads = reads
        self._random = random
        self._stop = threading.Event()
        self._current: Stream | None = None
        # A planned close loses nothing bazaar does not ring on the next stream, so only a first
        # open or a drop reads on ready.
        self._read_on_ready = True
        self.connected = False

    def serve_one(self) -> str:
        """One stream from open to end: "closed" when bazaar ended it as planned, else "failed"."""
        try:
            stream = self._open()
        except Exception as exc:
            log.debug("mail stream not opened: %s", exc)
            self._read_on_ready = True
            return "failed"
        self._current = stream
        ready = False
        try:
            for event, kind in stream.events():
                if event == "ready":
                    ready = self.connected = True
                    if self._read_on_ready:
                        self._read_on_ready = False
                        for each in KINDS:
                            self._read(each)
                elif kind is not None:
                    self._read(kind)
        except StreamMissing as exc:
            log.debug("no mail stream: %s", exc)
            ready = False
        except Exception as exc:
            log.info("mail stream dropped: %s", type(exc).__name__)
            ready = False
        finally:
            self.connected = False
            self._current = None
            stream.close()
        if not ready:
            self._read_on_ready = True
            return "failed"
        return "closed"

    def _read(self, kind: str) -> None:
        try:
            self._reads[kind].run()
        except Exception:
            # The five-minute read retries it; the stream keeps going.
            log.exception("%s read after a ring failed", kind)

    def run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            if self.serve_one() == "closed":
                failures = 0
                continue
            failures += 1
            self._stop.wait(reconnect_wait(failures, self._random))

    def shutdown(self) -> None:
        self._stop.set()
        stream = self._current
        if stream is not None:
            stream.close()
