"""The mail lanes driven by bazaar's ring stream, against a fake bazaar that streams."""

from __future__ import annotations

import http.server
import itertools
import random
import threading
import time

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sellee.rail import mail_stream
from sellee.rail.client import RailClient
from sellee.rail.mail_stream import (
    KINDS,
    MailStream,
    MailStreamConnection,
    SingleFlight,
    StreamMissing,
    reconnect_wait,
)

_PROPERTY = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
_SPREAD = settings(max_examples=300, deadline=None)


def _clock():
    """Ten seconds pass between looks, so every scripted stream outlasts MIN_PLANNED_SEC."""
    ticks = itertools.count()
    return lambda: 10.0 * next(ticks)


class FakeBazaar:
    """Mail by kind, and the reads that store whatever is not stored yet, as the lanes do."""

    def __init__(self):
        self.mail = {kind: [] for kind in KINDS}
        self.stored = {kind: [] for kind in KINDS}
        self.reads = 0

    def deliver(self, kind):
        self.mail[kind].append(f"{kind}-{len(self.mail[kind])}")

    def read(self, kind):
        self.reads += 1
        for mail in self.mail[kind]:
            if mail not in self.stored[kind]:
                self.stored[kind].append(mail)

    def all_stored(self):
        return all(self.stored[k] == self.mail[k] for k in KINDS)


class Connection:
    """One scripted stream: its steps run as the client reads it."""

    def __init__(self, bazaar, steps, ending, log):
        self.bazaar, self.steps, self.ending, self.log = bazaar, steps, ending, log

    def events(self):
        if self.ending == "missing":
            raise StreamMissing("no mail stream on this bazaar")
        yield ("ready", None)
        for kind, rung in self.steps:
            self.bazaar.deliver(kind)
            if rung:
                self.log.append(("ring", kind, len(self.bazaar.mail[kind])))
                yield ("ring", kind)
        if self.ending == "drop":
            raise OSError("connection reset")

    def close(self):
        pass


def _client(bazaar, connections, log):
    reads = {}
    for kind in KINDS:

        def read(kind=kind):
            bazaar.read(kind)
            log.append(("read", kind, len(bazaar.stored[kind])))

        reads[kind] = SingleFlight(read)
    pending = iter(connections)
    stream = MailStream(open_stream=lambda: next(pending), reads=reads, clock=_clock())
    return stream, reads


_steps = st.lists(st.tuples(st.sampled_from(KINDS), st.booleans()), max_size=5)
_connections = st.lists(
    st.tuples(_steps, st.sampled_from(["planned", "drop", "missing"])), min_size=1, max_size=6
)


@_PROPERTY
@given(_connections)
def test_rings_and_connects_read_what_they_should_and_the_timed_read_catches_the_rest(script):
    bazaar, log = FakeBazaar(), []
    connections = [Connection(bazaar, steps, ending, log) for steps, ending in script]
    client, reads = _client(bazaar, connections, log)
    rings = ready_reads = 0
    read_on_ready = True
    for steps, ending in script:
        before = bazaar.reads
        outcome = client.serve_one()
        if ending == "missing":
            assert outcome == "failed" and bazaar.reads == before
            read_on_ready = True
            continue
        if read_on_ready:
            ready_reads += 1
        rings += sum(1 for _, rung in steps if rung)
        assert outcome == ("closed" if ending == "planned" else "failed")
        read_on_ready = ending == "drop"
    # Every ring is followed by a read of its kind storing everything delivered before it.
    for i, entry in enumerate(log):
        if entry[0] == "ring":
            _, kind, delivered = entry
            assert any(e[0] == "read" and e[1] == kind and e[2] >= delivered for e in log[i + 1 :])
    # No idle reads: at most one per ring, two per reading connect.
    assert bazaar.reads <= rings + 2 * ready_reads
    # Catch-up: the timed read stores whatever no ring announced.
    for kind in KINDS:
        reads[kind].run()
    assert bazaar.all_stored()


def test_a_ready_after_a_planned_close_reads_nothing():
    bazaar, log = FakeBazaar(), []
    first = Connection(bazaar, [], "planned", log)
    second = Connection(bazaar, [], "planned", log)
    client, _ = _client(bazaar, [first, second], log)
    assert client.serve_one() == "closed"
    assert bazaar.reads == 2
    assert client.serve_one() == "closed"
    assert bazaar.reads == 2


def test_a_ready_after_a_drop_reads_both_kinds():
    bazaar, log = FakeBazaar(), []
    client, _ = _client(
        bazaar, [Connection(bazaar, [], "drop", log), Connection(bazaar, [], "planned", log)], log
    )
    client.serve_one()
    bazaar.deliver("relay")
    client.serve_one()
    assert bazaar.reads == 4
    assert bazaar.all_stored()


def test_a_ring_reads_only_its_kind():
    bazaar, log = FakeBazaar(), []
    client, _ = _client(
        bazaar,
        [Connection(bazaar, [("registration", True), ("relay", False)], "planned", log)],
        log,
    )
    client.serve_one()
    assert bazaar.stored["registration"] == bazaar.mail["registration"]
    assert bazaar.stored["relay"] == []


def test_a_stream_that_ends_at_once_is_a_failure_not_a_planned_close():
    bazaar, log = FakeBazaar(), []
    connection = Connection(bazaar, [], "planned", log)
    client = MailStream(
        open_stream=lambda: connection,
        reads={kind: SingleFlight(lambda: None) for kind in KINDS},
        clock=lambda: 0.0,
    )
    assert client.serve_one() == "failed"


def test_with_no_stream_the_retry_waits_as_long_as_the_timed_read():
    waits = []

    class Stop:
        def __init__(self):
            self.calls = 0

        def is_set(self):
            return self.calls > 0

        def wait(self, seconds):
            waits.append(seconds)
            self.calls += 1

    client = MailStream(
        open_stream=lambda: Connection(FakeBazaar(), [], "missing", []),
        reads={kind: SingleFlight(lambda: None) for kind in KINDS},
        rand=lambda: 0.5,
    )
    client._stop = Stop()  # type: ignore[assignment]
    client.run()
    assert waits == [mail_stream.UNAVAILABLE_WAIT_SEC]


def test_a_failed_read_does_not_end_the_stream():
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("rail down")

    connection = Connection(FakeBazaar(), [], "planned", [])
    client = MailStream(
        open_stream=lambda: connection,
        reads={kind: SingleFlight(broken) for kind in KINDS},
        clock=_clock(),
    )
    assert client.serve_one() == "closed"
    assert len(calls) == 2


@_SPREAD
@given(st.integers(min_value=1, max_value=40), st.randoms(use_true_random=False))
def test_reconnect_waits_spread_then_back_off_to_a_minute(failures, rng):
    wait = reconnect_wait(failures, rng.random)
    assert 1.0 <= wait <= 60.0
    if failures == 1:
        assert wait <= 30.0


def test_reconnect_waits_are_spread_across_installs():
    first = [reconnect_wait(1, random.Random(seed).random) for seed in range(200)]
    assert min(first) < 5 and max(first) > 25


def _held_read(fail_first=False):
    started, release = threading.Event(), threading.Event()
    runs = []

    def read():
        runs.append(1)
        if len(runs) == 1:
            started.set()
            release.wait(2)
            if fail_first:
                raise RuntimeError("rail down")

    return SingleFlight(read), started, release, runs


def test_a_ring_during_a_read_runs_exactly_one_more_read():
    flight, started, release, runs = _held_read()
    first = threading.Thread(target=flight.run)
    first.start()
    started.wait(2)
    flight.run()
    flight.run()
    release.set()
    first.join(2)
    assert len(runs) == 2


def test_a_ring_during_a_failing_read_still_runs_its_read():
    flight, started, release, runs = _held_read(fail_first=True)
    first = threading.Thread(target=lambda: flight.run())
    first.start()
    started.wait(2)
    flight.run()
    release.set()
    first.join(2)
    assert len(runs) == 2


# The real stream over HTTP: parsing what bazaar sends, and shutdown.


class _Bazaar(http.server.BaseHTTPRequestHandler):
    frames: list[bytes] = []
    status = 200
    chunked = False
    hold: threading.Event
    served: threading.Event

    def do_GET(self):
        if self.status != 200:
            self.send_response(self.status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if self.chunked:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for frame in self.frames:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(frame), frame) if self.chunked else frame)
            self.wfile.flush()
        self.served.set()
        self.hold.wait(5)
        if self.chunked:
            self.wfile.write(b"0\r\n\r\n")


def _serve(frames, *, status=200, hold=False, chunked=False):
    handler = type(
        "Handler",
        (_Bazaar,),
        {
            "frames": frames,
            "status": status,
            "chunked": chunked,
            "hold": threading.Event(),
            "served": threading.Event(),
            "protocol_version": "HTTP/1.1" if chunked else "HTTP/1.0",
            "log_message": lambda *a: None,
        },
    )
    if not hold:
        handler.hold.set()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = RailClient(
        api_base=f"http://127.0.0.1:{server.server_port}",
        api_key="baz_test",
        web_base_url="http://x",
    )
    return server, client, handler


_FRAMES = [
    b"event: ready\ndata: {}\n\n",
    b": keep-alive\n\n",
    b'event: ring\ndata: {"kind":"registration"}\n\n',
    b'event: ring\ndata: {"kind":"relay"}\n\n',
    b'event: ring\ndata: {"kind":"listings"}\n\n',
]


def test_the_client_reads_bazaars_events_and_skips_keep_alives():
    for chunked in (False, True):
        server, client, _ = _serve(_FRAMES, chunked=chunked)
        try:
            events = list(client.open_mail_stream().events())
            assert events == [("ready", None), ("ring", "registration"), ("ring", "relay")]
        finally:
            server.shutdown()


def test_a_bazaar_without_the_route_is_no_stream():
    for status in (404, 401):
        server, client, _ = _serve([], status=status)
        try:
            try:
                list(client.open_mail_stream().events())
            except StreamMissing:
                pass
            else:
                raise AssertionError(f"HTTP {status} is no stream")
        finally:
            server.shutdown()


def test_a_stream_closed_before_it_is_read_never_connects():
    server, client, handler = _serve(_FRAMES)
    try:
        stream = client.open_mail_stream()
        stream.close()
        assert list(stream.events()) == []
        assert not handler.served.is_set()
    finally:
        server.shutdown()


def test_shutdown_ends_a_waiting_stream_at_once():
    server, client, handler = _serve([b"event: ready\ndata: {}\n\n"], hold=True)
    reads = {kind: SingleFlight(lambda: None) for kind in KINDS}
    stream = MailStream(open_stream=client.open_mail_stream, reads=reads)
    runner = threading.Thread(target=stream.run)
    runner.start()
    try:
        assert handler.served.wait(2)
        time.sleep(0.1)
        started = time.monotonic()
        stream.shutdown()
        runner.join(5)
        assert not runner.is_alive()
        assert time.monotonic() - started < mail_stream.KEEP_ALIVE_SEC
    finally:
        handler.hold.set()
        server.shutdown()


def test_the_connection_uses_the_proxy_the_reads_use(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://proxy.example:3128")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    conn = MailStreamConnection(url="https://api.carousell.ai/api/v1/me/mail-stream", api_key="k")
    assert (conn._conn.host, conn._conn.port) == ("proxy.example", 3128)
    assert conn._conn._tunnel_host == "api.carousell.ai"
