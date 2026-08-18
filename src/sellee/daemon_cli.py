"""Dispatch for `sellee daemon <subcommand>`."""

from __future__ import annotations

import argparse
import sys

from sellee import deployment

# The verbs that register, start or stop a supervisor job. In a container there is no job — the
# process is the container's — and a command that pretended otherwise would either do nothing or
# leave the seller believing they had stopped an agent that is still running.
_SUPERVISOR_VERBS = frozenset({"install", "uninstall", "start", "stop"})


def dispatch(args: argparse.Namespace) -> int:
    command = args.daemon_command

    if command == "run":
        from sellee import daemon

        return daemon.run_daemon(once=args.once)

    if deployment.is_container() and command in _SUPERVISOR_VERBS:
        print(
            f"`daemon {command}` has nothing to do here: this process is the container's, and "
            "starting, stopping and removing it is your container runtime's job, not ours.",
            file=sys.stderr,
        )
        return 2

    from sellee import supervisor

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
