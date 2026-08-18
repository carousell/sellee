"""Command-line dispatch — one front door for the daemon, the logs, and version.

argparse subcommands over sys.argv; subcommand implementations are imported lazily so the
CLI module itself stays cheap to load and free of import cycles.
"""

from __future__ import annotations

import argparse

from sellee import __version__

# The one constant the parser's own help text needs; everything else is imported lazily.
UPDATE_AVAILABLE_EXIT = 10


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sellee")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="print the version and exit")

    setup = sub.add_parser("setup", help="install sellee on this machine")
    setup.add_argument(
        "--dev",
        action="store_true",
        help="point the install at this working tree instead of copying it into versions/",
    )
    setup.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="take the default answer to every question",
    )
    setup_mode = setup.add_mutually_exclusive_group()
    setup_mode.add_argument(
        "--login-start",
        dest="mode",
        action="store_const",
        const="login-start",
        help="don't ask: start the daemon at login",
    )
    setup_mode.add_argument(
        "--manual",
        dest="mode",
        action="store_const",
        const="manual",
        help="don't ask: start the daemon only on demand",
    )
    setup.add_argument("--region", default=None, help="two-letter region code (e.g. SG)")
    setup.add_argument(
        "--skip-markets", action="store_true", help="don't offer marketplace sign-in"
    )
    setup.add_argument("--skip-telegram", action="store_true", help="don't offer Telegram")
    setup.add_argument(
        "--no-modify-path",
        action="store_true",
        help="never edit a shell rc file; print the PATH line instead",
    )

    sub.add_parser("healthcheck", help="check the install (exit 1 if anything is wrong)")

    update = sub.add_parser("update", help="update to the latest release")
    update_what = update.add_mutually_exclusive_group()
    update_what.add_argument(
        "--check",
        action="store_true",
        help=f"report the available version and exit {UPDATE_AVAILABLE_EXIT} if there is one",
    )
    update_what.add_argument(
        "--rollback", action="store_true", help="go back to the previously installed version"
    )
    update.add_argument(
        "--url", default=None, help="base URL to fetch the release from (staging, testing)"
    )

    remove = sub.add_parser("uninstall", help="remove sellee from this machine")
    remove.add_argument(
        "--preserve-data",
        action="store_true",
        help="keep the database, photos, browser profile and the secrets they depend on",
    )
    remove.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

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
        help="start automatically at login (job file in the supervisor's auto-start dir)",
    )
    mode.add_argument(
        "--manual",
        dest="mode",
        action="store_const",
        const="manual",
        help="start only on demand (job file kept in the config dir)",
    )
    install.add_argument(
        "--label",
        default=None,
        help="override the supervisor job label (side-by-side dev testing)",
    )

    for name, helptext in (
        ("uninstall", "unregister the daemon and remove its job file"),
        ("start", "register the daemon with the supervisor"),
        ("stop", "unregister the daemon from the supervisor"),
        ("status", "report daemon state, mode, heartbeat, and recent events"),
    ):
        p = dsub.add_parser(name, help=helptext)
        p.add_argument("--label", default=None, help="override the supervisor job label")

    logs = sub.add_parser("logs", help="tail the event store")
    logs.add_argument("--follow", action="store_true", help="poll for new events (~1s)")
    logs.add_argument("--pass", dest="pass_id", default=None, help="filter by pass id")
    logs.add_argument("--since", default=None, help="only events newer than a duration (e.g. 30m)")
    logs.add_argument(
        "--kind",
        action="append",
        dest="kinds",
        default=None,
        help="filter by event kind (repeatable)",
    )
    logs.add_argument(
        "--json",
        action="store_true",
        help="emit NDJSON (one event object per line) instead of the human format",
    )
    logs.add_argument(
        "--all",
        action="store_true",
        help="include routine (heartbeat) events, hidden by default",
    )
    logs.add_argument(
        "--web",
        action="store_true",
        help="open the rendered web tail in a browser (needs the daemon)",
    )

    pass_cmd = sub.add_parser("pass", help="pass lifecycle")
    psub = pass_cmd.add_subparsers(dest="pass_command", required=True)
    prun = psub.add_parser("run", help="enqueue a pass via the running daemon")
    prun.add_argument("pass_type", help="pass type (e.g. publish)")
    prun.add_argument("--item", default=None, help="item id the pass operates on")
    prun.add_argument(
        "--market",
        default=None,
        help="marketplace to publish to (default: carousell-ai, the rail)",
    )
    prun.add_argument("--follow", action="store_true", help="tail the pass's events until it ends")

    sub.add_parser("chat", help="talk to Sellee in this terminal")

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

    connect = sub.add_parser("connect", help="connect a channel or sign in to a marketplace")
    consub = connect.add_subparsers(dest="connect_command", required=True)
    ctel = consub.add_parser("telegram", help="bind a Telegram bot (token read from stdin)")
    ctel.add_argument("--status", action="store_true", help="report bind status and exit")
    ctel.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="seconds to wait for /start (default: 300 interactive, 120 piped)",
    )
    # One per marketplace we can actually drive, so `connect --help` lists what exists rather
    # than accepting any word and failing at the door.
    from sellee.browser import markets as _markets

    for market in _markets.supported_markets():
        consub.add_parser(market, help=f"sign in to {market} in the agent's browser")

    settings_cmd = sub.add_parser("settings", help="view and change seller settings")
    ssub = settings_cmd.add_subparsers(dest="settings_command", required=True)
    ssub.add_parser("list", help="list current settings and any pending changes")
    sset = ssub.add_parser("set", help="set a setting now (no approval round-trip)")
    sset.add_argument("key", help="the setting key (from `settings list`)")
    sset.add_argument(
        "value",
        help="the new value as JSON — e.g. '[\"carousell\"]' or '[2300, 800]'; "
        "a bare word is taken as text",
    )
    for verb, helptext in (
        ("approve", "approve a held settings change by id"),
        ("cancel", "cancel a held settings change by id"),
        ("undo", "undo an applied settings change by id"),
    ):
        sp = ssub.add_parser(verb, help=helptext)
        sp.add_argument("change_id", help="the change id (from `settings list`)")

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

    if args.command == "setup":
        from sellee import setup_cli

        return setup_cli.run(args)

    if args.command == "healthcheck":
        from sellee import healthcheck

        return healthcheck.run(args)

    if args.command == "update":
        from sellee.installer import update as update_cli

        return update_cli.run(args)

    if args.command == "uninstall":
        from sellee import uninstall_cli

        return uninstall_cli.run(args)

    if args.command == "daemon":
        from sellee import daemon_cli

        return daemon_cli.dispatch(args)

    if args.command == "logs":
        from sellee import logs_cli

        return logs_cli.run(args)

    if args.command == "pass":
        from sellee import pass_cli

        return pass_cli.run(args)

    if args.command == "chat":
        from sellee import pass_cli

        return pass_cli.chat(args)

    if args.command == "harness":
        from sellee import pass_cli

        return pass_cli.harness_config(args.dir)

    if args.command == "connect":
        from sellee import connect_cli

        return connect_cli.run(args)

    if args.command == "settings":
        from sellee import settings_cli

        return settings_cli.run(args)

    if args.command == "provision":
        from sellee import pass_cli

        return pass_cli.provision(args)

    if args.command == "mcp-proxy":
        from sellee import mcp_proxy

        return mcp_proxy.main(args)

    raise AssertionError(f"unhandled command: {args.command}")  # pragma: no cover
