"""`sellee logs` — tail the event store over a read-only connection.

Reads events.db directly (WAL gives concurrent readers by construction), so it needs no
cooperation from the daemon and works whether or not the daemon is up — it can even tail a
stopped daemon's history. --follow polls seq > last on a ~1s cadence.

--web is the one branch that needs a live daemon: the rendered page is served by it, and its
URL carries the attended token, which is the whole reason this is a flag rather than something
a person is expected to paste together.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import webbrowser
from datetime import datetime

from sellee import paths
from sellee.db import connect_reader
from sellee.events import LEVEL_ORDER, Event, event_to_wire, level_for, query_events

_POLL_INTERVAL_SEC = 1.0
_LEVEL_RANK = {name: i for i, name in enumerate(LEVEL_ORDER)}
_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_DURATION_UNIT_SEC = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Flags the page has no equivalent for: it follows by default, isolates a pass through a UI pill,
# and its own raw-JSON view is what plain `logs --json` already gives a pipe. Silently ignoring
# them would read as "the filter applied".
_TERMINAL_ONLY_FLAGS = (
    ("--follow", "follow"),
    ("--pass", "pass_id"),
    ("--kind", "kinds"),
    ("--json", "json"),
    ("--all", "all"),
)


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


def _open_web(args: argparse.Namespace) -> int:
    """`sellee logs --web` — print the tail's URL, then open it."""
    from sellee import config, control

    conflicts = [flag for flag, dest in _TERMINAL_ONLY_FLAGS if getattr(args, dest, None)]
    if conflicts:
        print(
            f"--web composes only with --since (not {', '.join(conflicts)})",
            file=sys.stderr,
        )
        return 2
    if args.since:
        try:
            _parse_since(args.since)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    token = control.require_token()
    if not token:
        return 1
    port = config.load().http_port
    try:
        status, body = control.get(port, token, "/control/channel-status")
    except control.DaemonUnreachable:
        print(
            "sellee: the daemon serves the web tail — start it with "
            "`sellee daemon start` (or use `sellee logs` instead)",
            file=sys.stderr,
        )
        return 1
    if status != 200:
        print(f"sellee: {body.get('error', f'HTTP {status}')}", file=sys.stderr)
        return 1

    url = control.tail_url(port, token, args.since)
    # Printed before opening, and regardless of whether opening works: over SSH, or with no
    # browser to hand off to, the URL itself is the useful output.
    print(url)
    webbrowser.open(url)
    return 0


def run(args: argparse.Namespace) -> int:
    if getattr(args, "web", False):
        return _open_web(args)

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
    # By default hide the routine heartbeat; --all lifts the floor, and an explicit --kind is
    # itself an intentional request, so it shows whatever it asks for regardless of level.
    floor = _LEVEL_RANK["routine"] if (args.all or args.kinds) else _LEVEL_RANK["info"]
    filters = {"since_ts": since_ts, "pass_id": args.pass_id, "kinds": args.kinds}
    conn = connect_reader(db_path)
    try:
        last_seq = 0
        for event in query_events(conn, **filters):
            last_seq = event.seq  # advance even when filtered, else --follow re-queries it
            if _LEVEL_RANK[level_for(event.kind)] >= floor:
                # flush per line so a piped --follow surfaces events as they land, not in blocks
                print(fmt(event), flush=True)

        if not args.follow:
            return 0

        while True:
            time.sleep(_POLL_INTERVAL_SEC)
            for event in query_events(conn, after_seq=last_seq, **filters):
                last_seq = event.seq
                if _LEVEL_RANK[level_for(event.kind)] >= floor:
                    print(fmt(event), flush=True)
    except KeyboardInterrupt:
        return 0
    finally:
        conn.close()
