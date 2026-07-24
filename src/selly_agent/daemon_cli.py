"""Dispatch for `selly-agent daemon <subcommand>`."""

from __future__ import annotations

import argparse


def dispatch(args: argparse.Namespace) -> int:
    command = args.daemon_command

    if command == "run":
        from selly_agent import daemon

        return daemon.run_daemon(once=args.once)

    from selly_agent import supervisor

    if command == "install":
        return supervisor.install(mode=args.mode, label=args.label)
    if command == "uninstall":
        return supervisor.uninstall(label=args.label)
    if command == "start":
        return supervisor.start(label=args.label)
    if command == "stop":
        return supervisor.stop(label=args.label)
    if command == "status":
        return supervisor.status(label=args.label)

    raise AssertionError(f"unhandled daemon command: {command}")  # pragma: no cover
