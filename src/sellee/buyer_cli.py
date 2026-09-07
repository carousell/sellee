"""`sellee buyer` — open the buyer simulator.

A rehearsal surface, not a product one. It opens a page where the seller plays a buyer against
their own agent: the messages they type are written to a `simbuyer:` thread the same way the
browser inbox writes a real conversation, and everything the agent does in reply — the reply pass,
the model, the floor gate, minting a carousell.ai checkout link — is the real thing.

The daemon serves the page only when it was started with SELLEE_BUYER_SIM set, so the failure this
command most needs to explain well is a daemon that has it unset.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser

from sellee import config, control


def run(args: argparse.Namespace) -> int:
    token = control.require_token()
    if not token:
        return 1
    port = config.load().http_port

    try:
        status, body = control.get(port, token, "/control/sim-items")
    except control.DaemonUnreachable:
        print(
            "sellee: the daemon serves the buyer simulator — start it with "
            "`SELLEE_BUYER_SIM=1 sellee daemon start`",
            file=sys.stderr,
        )
        return 1
    if status == 404:
        # Two very different faults both answer 404, and telling them apart is the difference
        # between a one-word fix and an afternoon: the route exists but the flag is off, or the
        # daemon is a build that has no simulator in it at all. The handlers name themselves when
        # they refuse, so the body is what separates them — the router's generic "not found" means
        # the route was never registered.
        if body.get("error") == "buyer simulator is not enabled":
            print(
                "sellee: this daemon is running the simulator's code but was not started with it "
                "enabled.\n"
                "  Restart the daemon with SELLEE_BUYER_SIM set — see `make buyer-daemon`.\n"
                "  While it is on, replies to real marketplace threads are refused rather than "
                "delivered; it is a rehearsal mode, not something to leave running.",
                file=sys.stderr,
            )
        else:
            print(
                "sellee: this daemon has no buyer simulator in it — the route is not registered, "
                "so it is an older build rather than a disabled flag.\n"
                "  `sellee daemon start` runs the installed release, which will not have it; the "
                "simulator has to be the daemon you are developing.\n"
                "  Stop that daemon and run one from this checkout: `make buyer-daemon`.",
                file=sys.stderr,
            )
        return 1
    if status != 200:
        print(f"sellee: {body.get('error', f'HTTP {status}')}", file=sys.stderr)
        return 1
    if not body.get("items"):
        print(
            "sellee: no item is published on carousell.ai yet, and the checkout link can only be "
            "minted for one that is. Publish an item first.",
            file=sys.stderr,
        )
        return 1

    status, body = control.post(port, token, "/control/tail-ticket", {})
    if status != 200:
        print(f"sellee: {body.get('error', f'HTTP {status}')}", file=sys.stderr)
        return 1

    url = f"{control.base_url(port)}/buyer?ticket={body['ticket']}"
    # Printed before opening, and whether or not opening works: over SSH, or with no browser to
    # hand off to, the URL is the useful output.
    print(url)
    webbrowser.open(url)
    return 0
