"""`selly-agent inspect` — tail the event store over a read-only connection.

Reads events.db directly (WAL gives concurrent readers by construction), so it needs no
cooperation from the daemon and works whether or not the daemon is up — it can even tail a
stopped daemon's history. --follow polls seq > last on a ~1s cadence.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime

from selly_agent import paths
from selly_agent.db import connect_reader
from selly_agent.events import Event, event_to_wire, query_events

_POLL_INTERVAL_SEC = 1.0
_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_DURATION_UNIT_SEC = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_since(raw: str) -> float:
    match = _DURATION_RE.match(raw.strip())
    if not match:
        raise ValueError(f"--since must look like 30s / 15m / 2h / 1d, got {raw!r}")
    amount, unit = int(match.group(1)), match.group(2)
    return time.time() - amount * _DURATION_UNIT_SEC[unit]


def _format(event: Event) -> str:
    local = datetime.fromtimestamp(event.ts).strftime("%Y-%m-%d %H:%M:%S")
    pass_id = event.pass_id if event.pass_id is not None else "-"
    payload = json.dumps(event.payload, separators=(",", ":"), sort_keys=True)
    return f"{local}  {event.kind:<18} pass={pass_id}  {payload}"


def _format_ndjson(event: Event) -> str:
    # compact, no sort_keys — preserve the wire field order so @ts stays first
    return json.dumps(event_to_wire(event), separators=(",", ":"))


def run(args: argparse.Namespace) -> int:
    db_path = paths.events_db()
    if not db_path.exists():
        print("no events yet (the daemon has not run in this environment)", file=sys.stderr)
        return 0

    try:
        since_ts = _parse_since(args.since) if args.since else None
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    fmt = _format_ndjson if args.json else _format
    filters = {"since_ts": since_ts, "pass_id": args.pass_id, "kinds": args.kinds}
    conn = connect_reader(db_path)
    try:
        last_seq = 0
        for event in query_events(conn, **filters):
            # flush per line so a piped --follow surfaces events as they land, not in blocks
            print(fmt(event), flush=True)
            last_seq = event.seq

        if not args.follow:
            return 0

        while True:
            time.sleep(_POLL_INTERVAL_SEC)
            for event in query_events(conn, after_seq=last_seq, **filters):
                print(fmt(event), flush=True)
                last_seq = event.seq
    except KeyboardInterrupt:
        return 0
    finally:
        conn.close()
