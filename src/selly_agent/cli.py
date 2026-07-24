"""Command-line dispatch — one front door for the daemon, inspect, and version.

argparse subcommands over sys.argv; subcommand implementations are imported lazily so the
CLI module itself stays cheap to load and free of import cycles.
"""

from __future__ import annotations

import argparse

from selly_agent import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="selly-agent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="print the version and exit")

    daemon = sub.add_parser("daemon", help="daemon lifecycle")
    dsub = daemon.add_subparsers(dest="daemon_command", required=True)

    run = dsub.add_parser("run", help="run the daemon in the foreground")
    run.add_argument(
        "--once",
        action="store_true",
        help="lock, migrate, run a single tick, then stop cleanly",
    )

    install = dsub.add_parser("install", help="provision the layout and register the daemon")
    mode = install.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--login-start",
        dest="mode",
        action="store_const",
        const="login-start",
        help="start automatically at login (plist in ~/Library/LaunchAgents)",
    )
    mode.add_argument(
        "--manual",
        dest="mode",
        action="store_const",
        const="manual",
        help="start only on demand (plist kept in the config dir)",
    )
    install.add_argument(
        "--label",
        default=None,
        help="override the launchd label (side-by-side dev testing)",
    )

    for name, helptext in (
        ("uninstall", "unregister the daemon and remove its plist"),
        ("start", "register the daemon with launchd"),
        ("stop", "unregister the daemon from launchd"),
        ("status", "report daemon state, mode, heartbeat, and recent events"),
    ):
        p = dsub.add_parser(name, help=helptext)
        p.add_argument("--label", default=None, help="override the launchd label")

    inspect = sub.add_parser("inspect", help="tail the event store")
    inspect.add_argument("--follow", action="store_true", help="poll for new events (~1s)")
    inspect.add_argument("--pass", dest="pass_id", default=None, help="filter by pass id")
    inspect.add_argument(
        "--since", default=None, help="only events newer than a duration (e.g. 30m)"
    )
    inspect.add_argument(
        "--kind",
        action="append",
        dest="kinds",
        default=None,
        help="filter by event kind (repeatable)",
    )
    inspect.add_argument(
        "--json",
        action="store_true",
        help="emit NDJSON (one event object per line) instead of the human format",
    )

    pass_cmd = sub.add_parser("pass", help="pass lifecycle")
    psub = pass_cmd.add_subparsers(dest="pass_command", required=True)
    prun = psub.add_parser("run", help="enqueue a pass via the running daemon")
    prun.add_argument("pass_type", help="pass type (e.g. publish)")
    prun.add_argument("--item", default=None, help="item id the pass operates on")
    prun.add_argument("--follow", action="store_true", help="tail the pass's events until it ends")

    harness = sub.add_parser("harness", help="harness configuration")
    hsub = harness.add_subparsers(dest="harness_command", required=True)
    hconf = hsub.add_parser("config", help="write harness config for an attended session")
    hconf.add_argument(
        "--attended",
        action="store_true",
        required=True,
        help="write .mcp.json for an attended Claude Code session",
    )
    hconf.add_argument("--dir", default=None, help="destination directory (default: cwd)")

    connect = sub.add_parser("connect", help="connect an optional channel")
    consub = connect.add_subparsers(dest="connect_command", required=True)
    ctel = consub.add_parser("telegram", help="bind a Telegram bot (token read from stdin)")
    ctel.add_argument("--status", action="store_true", help="report bind status and exit")
    ctel.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="seconds to wait for /start (default: 300 interactive, 120 piped)",
    )

    provision = sub.add_parser("provision", help="provision an external rail")
    prsub = provision.add_subparsers(dest="provision_command", required=True)
    prov_ai = prsub.add_parser("carousell-ai", help="obtain the carousell.ai guest API key")
    prov_ai.add_argument("--region", default=None, help="two-letter region code (e.g. SG)")

    sub.add_parser("mcp-proxy", help="stdio<->HTTP MCP forwarder for stdio-only harnesses")

    return parser


def main(argv: list[str] | None = None) -> int:
    import sys

    args = _build_parser().parse_args((argv or sys.argv)[1:])

    if args.command == "version":
        print(__version__)
        return 0

    if args.command == "daemon":
        from selly_agent import daemon_cli

        return daemon_cli.dispatch(args)

    if args.command == "inspect":
        from selly_agent import inspect_cli

        return inspect_cli.run(args)

    if args.command == "pass":
        from selly_agent import pass_cli

        return pass_cli.run(args)

    if args.command == "harness":
        from selly_agent import pass_cli

        return pass_cli.harness_config(args)

    if args.command == "connect":
        from selly_agent import connect_cli

        return connect_cli.run(args)

    if args.command == "provision":
        from selly_agent import pass_cli

        return pass_cli.provision(args)

    if args.command == "mcp-proxy":
        from selly_agent import mcp_proxy

        return mcp_proxy.main(args)

    raise AssertionError(f"unhandled command: {args.command}")  # pragma: no cover
