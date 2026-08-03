"""`selly-agent healthcheck` — six questions, asked the same way every time.

Six, because these are the six things whose absence actually stops the agent: is the worker
running, can it reach you, can it drive the browser, can it spawn the browser server at all, can it
run a pass, and does it have a rail identity. Anything else a check could probe is either derived
from these or is not something a seller can act on, and a summary nobody reads because it is forty
lines long is worse than none.

Two rules shape it. Every check is a pure decision over inputs plus a wrapper that fetches them,
so a probe that throws becomes a failed line rather than the end of the report. And a check
fails only when something is actually wrong: an optional thing being off is a warning, and a
manual-mode daemon that is simply not started is the seller's own choice being reported back to
them, not a fault.
"""

from __future__ import annotations

import dataclasses
import shutil

from selly_agent import config, control, secrets, supervisor
from selly_agent.browser import client as browser_client
from selly_agent.installer import checks, preflight

# A heartbeat older than this means the scheduler loop is wedged rather than merely idle: it
# ticks every few seconds, so a minute of silence is not slow, it is stopped.
HEARTBEAT_STALE_SEC = 60.0


# --- pure decisions ----------------------------------------------------------------------------


def daemon_check(*, mode: str, registered: bool, heartbeat_age) -> checks.Check:
    """Is the worker running — judged against the mode the seller chose.

    Manual mode not running is informative, not a fault: they asked for a daemon they start
    themselves, and reporting their own decision as a failure trains people to ignore the report.
    """
    if registered:
        if heartbeat_age is None:
            return checks.fail(
                "daemon",
                "registered, but it has never written a heartbeat",
                "Something is failing at startup — see `selly-agent daemon status`.",
            )
        if heartbeat_age > HEARTBEAT_STALE_SEC:
            return checks.fail(
                "daemon",
                f"registered, but its last heartbeat was {heartbeat_age:.0f}s ago",
                "The loop looks stuck — `selly-agent daemon stop` then `daemon start`.",
            )
        return checks.ok("daemon", f"running (heartbeat {heartbeat_age:.0f}s ago)")
    if mode == supervisor.MANUAL:
        return checks.warn(
            "daemon",
            "not running — manual mode, which is what you chose",
            "selly-agent daemon start",
        )
    return checks.fail("daemon", "not running", "selly-agent daemon start")


def channel_check(*, bound: bool) -> checks.Check:
    """Telegram is optional by design, so its absence is never a failure — only a note about
    where escalations will wait instead."""
    if bound:
        return checks.ok("channel", "Telegram connected")
    return checks.warn(
        "channel",
        "no channel — anything needing you waits for your next session",
        "selly-agent connect telegram",
    )


def browser_check(*, enabled, blocked: str, states) -> checks.Check:
    """Can we drive the marketplaces the seller enabled, and are they still signed in?

    With no external marketplaces there is nothing to check and nothing wrong: the rail needs no
    browser. A probe the daemon declined to run is nothing wrong either — Chrome being closed or
    a pass mid-publish are both ordinary — but the report must say which reason held it up: the
    person reading it may be looking straight at a Chrome the report claims isn't there.
    """
    if not enabled:
        return checks.ok("browser", "no external marketplaces — carousell.ai only")
    if blocked:
        return checks.warn(
            "browser",
            f"{blocked}, so I can't check {len(enabled)} marketplace sign-in(s) right now",
            "Nothing to do — I'll check the next time I use the browser.",
        )
    logged_out = [row["market"] for row in states if row.get("state") == "logged_out"]
    unknown = [row["market"] for row in states if row.get("state") == "unknown"]
    if logged_out:
        names = ", ".join(logged_out)
        return checks.fail(
            "browser",
            f"signed out of {names}",
            f"selly-agent connect {logged_out[0]}",
        )
    if unknown:
        return checks.warn(
            "browser",
            f"Chrome is up; couldn't confirm the sign-in for {', '.join(unknown)}",
            "I'll confirm it the next time I list or read there.",
        )
    return checks.ok("browser", f"Chrome up, signed in to {len(states)} marketplace(s)")


def browser_server_check(*, binary: str, fragment: str, resolved) -> checks.Check:
    """Is the browser server's binary reachable from the PATH the worker is actually given?

    Deterministic and offline, unlike the browser check above — it resolves a name against a
    recorded PATH and nothing more. That is what makes it usable as an update's regression gate: a
    release that breaks the spawn fails here and gets rolled back, rather than going unnoticed until
    the next publish.

    No recorded fragment is not a fault: a daemon started from a shell inherits a PATH that already
    reaches node, and the installer records nothing in that case.
    """
    if not fragment:
        return checks.warn("browser server", "not checked — no recorded node path")
    if not resolved:
        return checks.fail(
            "browser server",
            f"{binary} is not reachable from the worker's PATH ({fragment})",
            "Node moved, or was replaced by a version manager — re-run ./setup to record where it "
            "lives now.",
        )
    return checks.ok("browser server", f"{binary} at {resolved}")


def rail_key_check(*, present: bool) -> checks.Check:
    if present:
        return checks.ok("carousell.ai key", "present")
    return checks.fail(
        "carousell.ai key",
        "missing — I can't list on carousell.ai without it",
        "selly-agent provision carousell-ai --region <XX>",
    )


# --- the probes ----------------------------------------------------------------------------------


def _daemon_probe(platform=None) -> checks.Check:
    status = supervisor.gather_status(platform=platform)
    return daemon_check(
        mode=status.mode,
        registered=status.registered,
        heartbeat_age=status.heartbeat_age_sec,
    )


def _channel_probe(platform=None) -> checks.Check:
    return channel_check(bound=supervisor.gather_status(platform=platform).channel_bound)


def _browser_probe(cfg) -> checks.Check:
    """Which marketplaces are enabled, and how each one's login looks — asked of the daemon.

    Both halves come from there because both need the store: which markets the seller enabled,
    and whether Chrome was already up (the daemon declines to probe when it was not, rather than
    opening a window for a status read).
    """
    token = secrets.read_mcp_token()
    if not token:
        return _browser_unknown("the daemon has never run here")
    try:
        status, answer = control.get(cfg.http_port, token, "/control/market-logins")
    except control.DaemonUnreachable:
        return _browser_unknown("the daemon isn't answering")
    if status != 200:
        return _browser_unknown(f"the daemon refused the read ({answer.get('error', status)})")
    return browser_check(
        enabled=answer.get("enabled") or [],
        blocked=answer.get("blocked") or "",
        states=answer.get("markets") or [],
    )


def _browser_unknown(reason: str) -> checks.Check:
    """With no daemon there is no way to know which marketplaces are enabled, and saying "none"
    would be a claim rather than an answer. Check ① already names the real problem."""
    return checks.warn("browser", f"not checked — {reason}")


def _browser_server_probe(cfg) -> checks.Check:
    """Resolve the server command's binary under the recorded fragment plus the supervised default —
    the same PATH the job definition carries, asked of nothing but the filesystem."""
    command = cfg.playwright_mcp_cmd or browser_client.default_command(
        browser_client.cdp_endpoint(cfg.chrome_cdp_port)
    )
    binary = str(command[0])
    fragment = cfg.node_bin_dir or ""
    return browser_server_check(
        binary=binary,
        fragment=fragment,
        resolved=shutil.which(binary, path=preflight.supervised_path(fragment)),
    )


def _harness_probe(cfg) -> checks.Check:
    # The same probe the installer gates on, under the name this report uses for it.
    return dataclasses.replace(preflight.check_claude(cfg), name="harness")


def _rail_key_probe() -> checks.Check:
    return rail_key_check(present=secrets.read_carousell_ai_api_key() is not None)


def run_checks(platform=None) -> list:
    """The six checks, in the order a person would ask them. None of them can raise."""
    cfg = config.load()
    return [
        checks.fail_open("daemon", lambda: _daemon_probe(platform)),
        checks.fail_open("channel", lambda: _channel_probe(platform)),
        checks.fail_open("browser", lambda: _browser_probe(cfg)),
        checks.fail_open("browser server", lambda: _browser_server_probe(cfg)),
        checks.fail_open("harness", lambda: _harness_probe(cfg)),
        checks.fail_open("carousell.ai key", _rail_key_probe),
    ]


def report(results) -> str:
    lines = checks.render(results)
    lines.append("")
    lines.append("All good." if checks.exit_code(results) == 0 else "Something needs attention.")
    return "\n".join(lines)


def run(args=None) -> int:
    results = run_checks()
    print(report(results))
    return checks.exit_code(results)
