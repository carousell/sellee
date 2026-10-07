"""The mail lanes driven by bazaar's ring stream, against a fake bazaar that streams."""

from __future__ import annotations

import http.server
import random
import threading
import time

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sellee.rail import mail_stream
from sellee.rail.client import RailClient
from sellee.rail.mail_stream import KINDS, MailStream, SingleFlight, StreamMissing, reconnect_wait

_PROPERTY = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


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

    def all_stored(self, kinds=KINDS):
        return all(self.stored[k] == self.mail[k] for k in kinds)


class Connection:
    """One scripted stream: its steps run as the client reads it."""

    def __init__(self, bazaar, steps, ending, log):
        self.bazaar, self.steps, self.ending, self.log = bazaar, steps, ending, log

    def events(self):
        if self.ending == "missing":
            raise StreamMissing("no mail stream on this bazaar")
        yield ("ready", None)
        for step in self.steps:
            kind, rung = step
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
    return MailStream(open_stream=lambda: next(pending), reads=reads), reads


_steps = st.lists(st.tuples(st.sampled_from(KINDS), st.booleans()), max_size=5)
_connections = st.lists(
    st.tuples(_steps, st.sampled_from(["planned", "drop", "missing"])), min_size=1, max_size=6
)


@_PROPERTY
@given(_connections)
def test_rings_and_connects_read_what_they_should_and_the_five_minute_read_catches_the_rest(script):
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
    # Catch-up: the 5-minute read stores whatever no ring announced.
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


def test_a_failed_read_does_not_end_the_stream():
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("rail down")

    connection = Connection(FakeBazaar(), [], "planned", [])
    client = MailStream(
        open_stream=lambda: connection,
        reads={kind: SingleFlight(broken) for kind in KINDS},
    )
    assert client.serve_one() == "closed"
    assert len(calls) == 2


@settings(max_examples=300, deadline=None)
@given(st.integers(min_value=1, max_value=40), st.randoms(use_true_random=False))
def test_reconnect_waits_spread_then_back_off_to_a_minute(failures, rng):
    wait = reconnect_wait(failures, rng.random)
    assert 1.0 <= wait <= 60.0
    if failures == 1:
        assert wait <= 30.0


def test_reconnect_waits_are_spread_across_installs():
    first = [reconnect_wait(1, random.Random(seed).random) for seed in range(200)]
    assert min(first) < 5 and max(first) > 25


def test_a_ring_during_a_read_runs_exactly_one_more_read():
    started, release = threading.Event(), threading.Event()
    runs = []

    def read():
        runs.append(1)
        if len(runs) == 1:
            started.set()
            release.wait(2)

    flight = SingleFlight(read)
    first = threading.Thread(target=flight.run)
    first.start()
    started.wait(2)
    flight.run()
    flight.run()
    release.set()
    first.join(2)
    assert len(runs) == 2


# The real stream over HTTP: parsing what bazaar sends, and shutdown.


class _Bazaar(http.server.BaseHTTPRequestHandler):
    frames: list[bytes] = []
    hold = threading.Event()
    status = 200

    def do_GET(self):
        if self.status != 200:
            self.send_response(self.status)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for frame in self.frames:
            self.wfile.write(frame)
            self.wfile.flush()
        self.hold.wait(5)

    def log_message(self, *args):
        pass


def _serve(frames, *, status=200, hold=False):
    handler = type(
        "Handler", (_Bazaar,), {"frames": frames, "status": status, "hold": threading.Event()}
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


def test_the_client_reads_bazaars_events_and_skips_keep_alives():
    server, client, _ = _serve(
        [
            b"event: ready\ndata: {}\n\n",
            b": keep-alive\n\n",
            b'event: ring\ndata: {"kind":"registration"}\n\n',
            b'event: ring\ndata: {"kind":"relay"}\n\n',
            b'event: ring\ndata: {"kind":"listings"}\n\n',
        ]
    )
    try:
        stream = client.open_mail_stream()
        assert list(stream.events()) == [
            ("ready", None),
            ("ring", "registration"),
            ("ring", "relay"),
        ]
    finally:
        server.shutdown()


def test_a_bazaar_without_the_route_is_no_stream():
    server, client, _ = _serve([], status=404)
    try:
        stream = client.open_mail_stream()
        try:
            list(stream.events())
        except StreamMissing:
            pass
        else:
            raise AssertionError("a 404 is no stream")
    finally:
        server.shutdown()


def test_shutdown_ends_a_waiting_stream_at_once():
    server, client, handler = _serve([b"event: ready\ndata: {}\n\n"], hold=True)
    reads = {kind: SingleFlight(lambda: None) for kind in KINDS}
    stream = MailStream(open_stream=client.open_mail_stream, reads=reads)
    runner = threading.Thread(target=stream.run)
    runner.start()
    try:
        deadline = time.monotonic() + 2
        while stream.connected is False and time.monotonic() < deadline:
            time.sleep(0.01)
        assert stream.connected
        started = time.monotonic()
        stream.shutdown()
        runner.join(5)
        assert not runner.is_alive()
        assert time.monotonic() - started < mail_stream.KEEP_ALIVE_SEC
    finally:
        handler.hold.set()
        server.shutdown()
