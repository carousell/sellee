"""Hold bazaar's mail stream open and read relay or registration mail when it rings. A ring is a
hint: the reads also run on a first open, after a dropped stream, and on the daemon's timer."""

from __future__ import annotations

import http.client
import json
import logging
import random
import socket
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from typing import Literal, Protocol

log = logging.getLogger(__name__)

Kind = Literal["relay", "registration"]
KINDS: tuple[Kind, ...] = ("relay", "registration")
Outcome = Literal["closed", "failed"]

KEEP_ALIVE_SEC = 20.0
# A stream silent for two keep-alives is treated as dropped.
READ_TIMEOUT_SEC = 2 * KEEP_ALIVE_SEC + 5
# bazaar closes a stream after 25 seconds; one ending sooner is a fault, not a planned close.
MIN_PLANNED_SEC = 5.0
FIRST_WAIT_MAX_SEC = 30.0
WAIT_CAP_SEC = 60.0
# No stream, or a key bazaar refuses: retry as often as the timed read runs, not every minute.
UNAVAILABLE_WAIT_SEC = 300.0
_CLIENT_UA = "SELLEE-rail/1"


class StreamMissing(Exception):
    """bazaar answered without a mail stream, or refused the key; the timed reads carry the mail."""


class Stream(Protocol):
    def events(self) -> Iterator[tuple[str, Kind | None]]: ...

    def close(self) -> None: ...


def parse_events(lines: Iterable[str]) -> Iterator[tuple[str, Kind | None]]:
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


class MailStreamConnection:
    """One mail stream over HTTP. `close` from another thread ends a read blocked on it at once,
    and a stream closed before it is read never connects."""

    def __init__(self, *, url: str, api_key: str, timeout_sec: float = READ_TIMEOUT_SEC):
        parts = urllib.parse.urlsplit(url)
        https = parts.scheme == "https"
        cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
        host = parts.hostname or ""
        port = parts.port or (443 if https else 80)
        # Through the proxy the MCP reads use (urllib honours it; http.client does not).
        proxy = urllib.request.getproxies().get(parts.scheme)
        if proxy and not urllib.request.proxy_bypass(host):
            via = urllib.parse.urlsplit(proxy)
            self._conn = cls(via.hostname or "", via.port, timeout=timeout_sec)
            self._conn.set_tunnel(host, port)
        else:
            self._conn = cls(host, port, timeout=timeout_sec)
        self._path = parts.path
        self._api_key = api_key
        self._lock = threading.Lock()
        self._closed = False

    def events(self) -> Iterator[tuple[str, Kind | None]]:
        with self._lock:
            if self._closed:
                return
        self._conn.connect()
        with self._lock:
            if self._closed:
                self._conn.close()
                return
        self._conn.request(
            "GET",
            self._path,
            headers={
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {self._api_key}",
                "User-Agent": _CLIENT_UA,
            },
        )
        resp = self._conn.getresponse()
        if resp.status != 200 or "text/event-stream" not in (resp.getheader("Content-Type") or ""):
            raise StreamMissing(f"mail stream answered HTTP {resp.status}")
        yield from parse_events(line.decode("utf-8", "replace") for line in resp)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            sock = self._conn.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self._conn.close()


class SingleFlight:
    """Runs func on one thread at a time; a call while it runs becomes one more run after it, even
    when that run fails."""

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
                    if self._again:
                        self._again = False
                        continue
                    self._running = False
                raise
            with self._lock:
                if not self._again:
                    self._running = False
                    return
                self._again = False


def reconnect_wait(failures: int, rand: Callable[[], float] = random.random) -> float:
    """Seconds to wait after the nth failure in a row: 1–30 at random, doubling, at most 60, so
    installs dropped together do not all come back at once."""
    first = 1.0 + (FIRST_WAIT_MAX_SEC - 1.0) * rand()
    # Six doublings pass the cap from any start; a larger exponent would overflow a float.
    return min(WAIT_CAP_SEC, first * 2 ** min(failures - 1, 6))


class MailStream:
    """bazaar's mail stream, read on its own thread: `run` until `shutdown`."""

    def __init__(
        self,
        *,
        open_stream: Callable[[], Stream],
        reads: dict[Kind, SingleFlight],
        rand: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._open = open_stream
        self._reads = reads
        self._rand = rand
        self._clock = clock
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._current: Stream | None = None
        # A planned close loses nothing bazaar does not ring on the next stream, so only a first
        # open or a drop reads on ready.
        self._read_on_ready = True
        self._unavailable = False

    def serve_one(self) -> Outcome:
        """One stream from open to end: "closed" when bazaar ended it as planned, else "failed"."""
        self._unavailable = False
        if self._stop.is_set():
            return "failed"
        try:
            stream = self._open()
        except Exception as exc:
            log.debug("mail stream not opened: %s", exc)
            self._read_on_ready = True
            return "failed"
        with self._lock:
            self._current = stream
        if self._stop.is_set():
            stream.close()
        opened, ready = self._clock(), False
        try:
            for event, kind in stream.events():
                if event == "ready":
                    ready = True
                    if self._read_on_ready:
                        self._read_on_ready = False
                        for each in KINDS:
                            self._read(each)
                elif kind is not None:
                    self._read(kind)
        except StreamMissing as exc:
            log.debug("no mail stream: %s", exc)
            self._unavailable = True
        except Exception as exc:
            log.info("mail stream dropped: %s", type(exc).__name__)
            ready = False
        finally:
            with self._lock:
                self._current = None
            stream.close()
        if not ready or self._clock() - opened < MIN_PLANNED_SEC:
            self._read_on_ready = True
            return "failed"
        return "closed"

    def _read(self, kind: Kind) -> None:
        try:
            self._reads[kind].run()
        except Exception:
            # The timed read retries it; the stream keeps going.
            log.exception("%s read after a ring failed", kind)

    def run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            if self.serve_one() == "closed":
                failures = 0
                continue
            failures += 1
            if self._unavailable:
                wait = UNAVAILABLE_WAIT_SEC * (0.8 + 0.4 * self._rand())
            else:
                wait = reconnect_wait(failures, self._rand)
            self._stop.wait(wait)

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            stream = self._current
        if stream is not None:
            stream.close()
