"""`selly-agent setup` — the deterministic installer, start to finish, in one terminal.

No model is involved anywhere in this file. Every phase is a machine step with a known answer,
and every phase is idempotent off real state rather than off a sentinel file, so a re-run after
a failure resumes instead of refusing or double-applying.

The ordering is load-bearing. Identity and auth gates come before slow work, so a machine that
cannot possibly finish fails in seconds rather than after a package download. And the daemon
comes up before anything that needs it: the attended token is minted at first start, and the
region, provisioning and marketplace phases all reach the daemon over its control routes rather
than writing state behind its back.
"""

from __future__ import annotations

import json
import time

from selly_agent import (
    __version__,
    config,
    connect_cli,
    control,
    healthcheck,
    heartbeat,
    host,
    marketplaces,
    pass_cli,
    passes,
    paths,
    secrets,
    settings_cli,
    supervisor,
)
from selly_agent.browser import markets as market_adapters
from selly_agent.installer import checks, materialize, preflight, runtime
from selly_agent.installer import region as region_guess
from selly_agent.installer.ui import Abort, Ui
from selly_agent.platform import get_platform

# How long the daemon gets to write its first heartbeat after being started. Startup is
# migrations plus a bind, so seconds; this is the "something is wrong" boundary, not a target.
DAEMON_READY_TIMEOUT_SEC = 60.0
# How much of the daemon's stderr to show when it does not come up — enough for a traceback.
_LOG_TAIL_LINES = 20


def run(args) -> int:
    ui = Ui(assume_yes=getattr(args, "yes", False))
    try:
        _run(args, ui)
    except Abort as exc:
        ui.fatal(exc)
        return 1
    except materialize.LayoutError as exc:
        ui.fatal(Abort(exc.message, exc.fix))
        return 1
    except runtime.RuntimeSetupError as exc:
        ui.fatal(Abort(str(exc), "Check the network and disk space, then run setup again."))
        return 1
    except control.DaemonUnreachable as exc:
        ui.fatal(
            Abort(
                f"the background worker stopped answering ({exc})",
                _daemon_diagnostics(),
            )
        )
        return 1
    return 0


def _run(args, ui: Ui) -> None:
    # Before the banner: on an OS we do not support, a friendly wall of paths we will never
    # write is worse than one honest line.
    platform_check = preflight.check_platform()
    if platform_check.failed:
        raise Abort(platform_check.detail, platform_check.fix)
    platform = get_platform()
    tree = materialize.source_tree()

    _intro(ui, platform)
    if not _agreed_to_proceed(ui):
        return
    _gates(ui, tree)
    _install_layout(ui, args, platform, tree)
    _start_daemon(ui, args, platform)

    # Everything past here talks to the running daemon, so it needs the token minted at its
    # first start. Read now rather than at import: before this line there was none.
    port = config.load().http_port
    token = secrets.read_mcp_token()
    if not token:
        raise Abort("the daemon is running but minted no attended token", _daemon_diagnostics())

    region = _seller_region(ui, args, port, token)
    _provision_rail(ui, region)
    _connect_markets(ui, args, port, token, region)
    _offer_telegram(ui, args, port, token)
    _attended_workspace(ui)
    _finish(ui, platform)


# --- what this is, and what it will touch ---------------------------------------------------


def _intro(ui: Ui, platform) -> None:
    ui.banner(__version__)
    ui.say("")
    ui.say("Selly is a marketplace agent: it lists items, answers buyers, and negotiates within")
    ui.say(f"limits you set. This installs version {__version__} on this machine.")
    ui.say("")
    ui.say("The installer will:")
    ui.say("  • check for Node, Chrome, and the claude CLI (installed and signed in)")
    ui.say("  • install this version, plus the `selly-agent` command")
    ui.say("  • register and start the background worker")
    ui.say("  • record the region and currency to price in")
    ui.say("  • optionally connect marketplaces and Telegram")
    ui.say("")
    ui.say("Selly will be installed into the following locations:")
    for line in materialize.layout_preview(platform=platform):
        ui.say(line)

    agent_var = preflight.agent_context()
    if agent_var and ui.interactive:
        # A TTY exists, but an agent is holding it. Questions asked here would be answered by
        # a model rather than the seller, so the run takes its defaults and says so.
        ui.say("")
        ui.warn(f"Running inside an agent session (${agent_var}) — questions take their defaults.")
        ui.interactive = False


def _agreed_to_proceed(ui: Ui) -> bool:
    """The consent gate: nothing has been written before this, and nothing is until it passes.

    Every phase after this one either writes to disk or installs something, including the
    dependency gates (a `brew install`, a package download). So the whole account of what will
    happen is given first, and one answer covers it — the individually consequential steps
    (touching a shell rc, signing in, opening a marketplace) still ask again in their own words.
    """
    ui.say("")
    ui.say("Nothing has been written yet.")
    if ui.confirm("Proceed with the installation?", default=True):
        return True
    ui.say("")
    ui.say(f"Cancelled — nothing was written. Re-run {preflight.setup_door()} when you're ready.")
    return False


# --- the gates ------------------------------------------------------------------------------


def _gates(ui: Ui, tree) -> None:
    ui.step("Checking this machine")
    cfg = config.load()

    _require(ui, checks.fail_open("install location", lambda: preflight.check_tree_location(tree)))
    _require(ui, checks.fail_open("python runtime", lambda: preflight.check_runtime(tree)))
    _require(ui, checks.fail_open("state store", lambda: preflight.check_state_store()))
    _gate_claude(ui, cfg)
    _gate_dependency(ui, "node", lambda: preflight.check_node())
    _gate_dependency(ui, "chrome", lambda: preflight.check_chrome(cfg.chrome_bin))

    # After the node gate, which is the friendly one (it names `brew install node`). This is the
    # authoritative one: it asks whether the *worker* can spawn the browser server, under the PATH
    # the worker will actually have rather than this shell's.
    _require(ui, checks.fail_open("browser server", lambda: preflight.check_supervised_spawn(cfg)))

    ui.note("fetching the browser server package (the first run downloads it)…")
    _report(ui, checks.fail_open("playwright", lambda: preflight.prewarm_playwright(cfg)))


def _report(ui: Ui, check: checks.Check) -> checks.Check:
    for line in check.render():
        ui.say(line)
    return check


def _require(ui: Ui, check: checks.Check) -> None:
    _report(ui, check)
    if check.failed:
        raise Abort(f"{check.name}: {check.detail}", check.fix)


def _gate_claude(ui: Ui, cfg) -> None:
    """The harness must be installed and signed in — offering the login flow until it is.

    Signed-out-but-installed is the failure the internal test round produced most, and it is
    invisible until the first pass spawns hours later, so it is settled here.
    """
    while True:
        check = checks.fail_open("claude CLI", lambda: preflight.check_claude(cfg))
        _report(ui, check)
        if not check.failed:
            return
        if passes.resolve_claude_bin(cfg) is None:
            # Installing it is a `curl | bash` of someone else's script: their call, not ours.
            raise Abort("the claude CLI is not installed", check.fix)
        if not ui.interactive:
            # `--yes` cannot stand in for a person here: the login is an interactive OAuth flow
            # that prints a URL and reads back a pasted code. With no terminal there is nobody
            # to hand it to.
            raise Abort("the claude CLI is signed out", check.fix)
        if not ui.confirm("Sign in to Claude now?", default=True, lead=False):
            raise Abort("the claude CLI is signed out", check.fix)
        ui.say("Running `claude auth login`. Return here when it finishes.")
        preflight.claude_login(cfg)


def _gate_dependency(ui: Ui, name: str, probe) -> None:
    """A dependency we can offer to install, once.

    Homebrew is never bootstrapped — piping a remote installer into a shell is a trust decision the
    machine's owner owns — so a Mac without it gets the instruction instead of an offer. winget
    ships with Windows, so there the offer is always available.
    """
    check = _report(ui, checks.fail_open(name, probe))
    if not check.failed:
        return

    command = preflight.install_command(name)
    if not command:
        raise Abort(
            f"{name}: {check.detail}",
            f"{check.fix}\n(Nothing here can install it for you — install {name} however you "
            f"prefer, then re-run {preflight.setup_door()}.)",
        )
    manager = preflight.package_manager_name()
    if not ui.confirm(f"Install {name} with {manager} now?", default=True, lead=False):
        raise Abort(f"{name}: {check.detail}", check.fix)

    ui.say(f"Running `{' '.join(command)}` — this can take a few minutes…")
    ok, detail = preflight.install_dependency(name)
    if not ok:
        raise Abort(f"installing {name} failed: {detail}", check.fix)
    _require(ui, checks.fail_open(name, probe))


# --- the layout -----------------------------------------------------------------------------


def _install_layout(ui: Ui, args, platform, tree) -> None:
    # A re-run replaces the very directory a running daemon is executing out of, so it is stopped
    # first. Skipping this is not merely untidy: `launchctl bootstrap` is a no-op on a label that
    # is already loaded, so the old process would keep running — still ticking heartbeats, so the
    # wait below would pass — while setup reported the new version as up.
    ui.step(f"Installing Selly {__version__}")
    if supervisor.gather_status(platform=platform).registered:
        ui.note("stopping the running worker so it picks up this version…")
        supervisor.stop(platform=platform)

    if args.dev:
        materialize.install_dev(tree)
        ui.say(f"dev mode: current {checks.arrow()} {tree}")
        ui.note("edits in that tree are live after a worker restart")
    else:
        dest = materialize.install_version(tree, __version__)
        ui.say(f"{dest}")
        removed = materialize.prune_versions()
        if removed:
            ui.note(f"removed older version(s): {', '.join(removed)}")

    shim = materialize.install_shim()
    ui.say(f"command: {shim}")
    _offer_path(ui, args)
    _record_claude_bin(ui)
    _record_node_bin_dir(ui)


def _offer_path(ui: Ui, args) -> None:
    """Make `selly-agent` findable, or say exactly why it is not.

    The uv/rustup convention: install into the user's own bin dir, then *offer* to touch the
    shell rc with an explicit way to decline — never silently edit dotfiles, and never silently
    leave a command that is not found.
    """
    if materialize.user_bin_on_path():
        return
    bin_dir = paths.user_bin_dir()
    export_line = materialize.RC_BLOCK_BODY
    ui.say("")
    ui.warn(f"{bin_dir} is not on your PATH, so `selly-agent` will not be found yet.")

    if host.windows():
        _offer_user_path_entry(ui, args, bin_dir)
        return

    rc_path = materialize.shell_rc_target()
    if rc_path is None:
        # A shell we do not write config for. Someone running something other than the macOS
        # default configured it deliberately, and an installer that rewrites that — or worse,
        # writes a file the shell never reads — is worse than one that says what to add.
        _explain_path_by_hand(ui, materialize.shell_name(), bin_dir)
        return

    # Editing a dotfile needs a signal that someone agreed to it: either a person who can answer,
    # or `--yes`, which is that agreement given up front. A plain piped run has neither, so it
    # gets the line to paste rather than a surprise edit.
    consented = ui.interactive or ui.assume_yes
    if args.no_modify_path or not consented:
        ui.say("Add this to your shell's startup file:")
        ui.say(export_line)
        return

    if not ui.confirm(f"Add it to {rc_path}?", default=True, lead=False):
        ui.say("Left unchanged. Add this to your shell's startup file:")
        ui.say(export_line)
        return

    if materialize.add_rc_block(rc_path):
        ui.say(f"added to {rc_path} — open a new terminal, or run: source {rc_path}")
    else:
        ui.say(f"{rc_path} already had it")


def _offer_user_path_entry(ui: Ui, args, bin_dir) -> None:
    """The Windows equivalent of the rc-file offer: the account's own PATH, in the registry.

    Same contract as the dotfile — offered, declinable, and recorded so an uninstall removes only
    what was added. There is no shell to ask about here: one value serves every terminal.
    """
    # Deliberately not a command to paste. `setx PATH "%PATH%;..."` does not expand %PATH% in
    # PowerShell, so following it replaces the account's entire PATH with that literal; run from
    # cmd it copies the combined machine and user PATH into the user value and truncates it at
    # 1024 characters. There is no one-liner here worth the chance of destroying someone's PATH.
    step = checks.arrow()
    by_hand = (
        f"Add this directory to your PATH: {bin_dir}\n"
        f"  (Settings {step} System {step} About {step} Advanced system settings {step} "
        f"Environment Variables,\n"
        f"   then edit Path under your user variables — or re-run setup and accept the offer.)"
    )
    consented = ui.interactive or ui.assume_yes
    if args.no_modify_path or not consented:
        ui.say(by_hand)
        return
    if not ui.confirm("Add it to your account's PATH?", default=True, lead=False):
        ui.say(f"Left unchanged. {by_hand}")
        return
    if materialize.add_user_path_entry():
        config.merge_into_file({"path_entry_added": True})
        ui.say("added to your account's PATH — open a new terminal to pick it up")
    else:
        ui.say("your account's PATH already had it")


def _explain_path_by_hand(ui: Ui, shell: str, bin_dir) -> None:
    """Say how to put the bin dir on the invoking shell's PATH, in that shell's own terms."""
    if shell == "fish":
        ui.say("Fish shell detected; its config is left unchanged. Run this once:")
        ui.say(materialize.FISH_COMMAND)
        ui.note(f"or in ~/.config/fish/config.fish:  {materialize.FISH_CONFIG_LINE}")
        return
    named = f"Shell: {shell}." if shell else "The shell could not be determined."
    ui.say(f"{named} Add {bin_dir} to PATH to run `selly-agent` by name.")


def _record_claude_bin(ui: Ui) -> None:
    """Pin the resolved `claude` path into config.

    The daemon runs under launchd with a minimal PATH, so "whatever `claude` resolves to in an
    interactive shell" is not something it can look up later. Resolved once, here, where a real
    shell's PATH is available.
    """
    resolved = passes.resolve_claude_bin(config.load())
    if resolved is None:
        return
    config.merge_into_file({"claude_bin": resolved})
    ui.note(f"claude: {resolved}")


def _record_node_bin_dir(ui: Ui) -> None:
    """Pin the PATH fragment reaching node and npx into config.

    Same reason as the harness path: the background worker is started by the supervisor with a
    minimal PATH, which carries no version manager's shims. Resolved here, in a real shell, where
    the answer is actually available — and it is the supervisor's job definition that carries it,
    so the worker's browser server can be spawned at all.
    """
    resolved = preflight.node_path_fragment()
    if not resolved:
        return
    config.merge_into_file({"node_bin_dir": resolved})
    ui.note(f"node: {resolved}")


# --- the daemon -----------------------------------------------------------------------------


def _start_daemon(ui: Ui, args, platform) -> None:
    if args.mode:
        # The mode came from a flag, so no question opens this phase — it needs its own heading.
        ui.step("Background worker")
        mode = args.mode
    else:
        mode = _ask_login_mode(ui)
    started_after = time.time()

    if supervisor.install(mode=mode, platform=platform) != 0:
        raise Abort("could not register the background worker (see the message above)")
    if mode == supervisor.MANUAL:
        ui.say("Manual mode: starting it now, but it will not restart after you log out.")
        ui.say("Run `selly-agent daemon start` when you want it.")
        supervisor.start(platform=platform)

    ui.note("waiting for the first heartbeat…")
    if not _wait_for_daemon(started_after):
        raise Abort(
            "the background worker didn't start",
            _daemon_diagnostics(),
        )
    ui.say("worker is up")


def _ask_login_mode(ui: Ui) -> str:
    if ui.confirm("Start the worker automatically when you log in?", default=True):
        return supervisor.LOGIN_START
    return supervisor.MANUAL


def _wait_for_daemon(started_after: float) -> bool:
    return heartbeat.wait_fresh(
        paths.heartbeat_path(),
        newer_than=started_after,
        timeout_sec=DAEMON_READY_TIMEOUT_SEC,
    )


# --- where the seller sells ------------------------------------------------------------------


def _seller_region(ui: Ui, args, port: int, token: str):
    """Record region, currency and timezone, and answer with the region the daemon now holds.

    The machine's timezone already implies all three, so this confirms a proposal rather than
    conducting an interview. A machine that implies nothing (or a seller who says no) is asked.
    Provisioning and the marketplace list both key off the answer, so it is read back from the
    daemon rather than assumed — on a re-run the region may already be there.
    """
    known = _stored_basics(port, token)
    if known.get("region") and not args.region:
        ui.step("Where you sell")
        ui.say(f"{region_guess.render(known)} — already recorded, unchanged")
        return known["region"]

    basics = _basics_from_flag(args) if args.region else region_guess.guess()
    if args.region:
        ui.step("Where you sell")
    elif basics and not ui.confirm(
        f"You sell in {region_guess.render(basics)}, correct?", default=True
    ):
        basics = None
    if basics is None:
        basics = _ask_basics(ui)
    if not basics:
        ui.warn("No region recorded — carousell.ai setup and the marketplace list are skipped.")
        ui.note("both are completed once a region is set")
        return None

    status, body = control.post(port, token, "/control/seller-basics", basics)
    if status != 200:
        raise Abort(f"could not record your region: {body.get('error', status)}")
    ui.say(f"recorded: {region_guess.render(body['basics'])}")
    return body["basics"].get("region")


def _stored_basics(port: int, token: str) -> dict:
    try:
        status, body = control.get(port, token, "/control/seller-basics")
    except control.DaemonUnreachable:
        return {}
    return (body.get("basics") or {}) if status == 200 else {}


def _basics_from_flag(args) -> dict:
    code = str(args.region).strip().upper()
    basics = {"region": code, "timezone": region_guess.system_timezone()}
    currency = region_guess.CURRENCIES.get(code)
    if currency:
        basics["currency"] = currency
    return {key: value for key, value in basics.items() if value}


def _ask_basics(ui: Ui):
    """Ask which country outright. Answers nothing when there is nobody to ask.

    Only the countries the rail serves are offered, and an answer outside them is refused here
    rather than three questions later at the door — the currency and timezone are not worth
    collecting for a region that cannot be stored.
    """
    if not ui.interactive:
        return None
    supported = region_guess.supported()
    code = ui.choose("Which country do you sell in?", supported)
    region = supported[code]
    timezone = ui.ask("Timezone?", default=region_guess.system_timezone(), lead=False).strip()
    basics = {
        "region": region,
        "currency": region_guess.CURRENCIES.get(region, ""),
        "timezone": timezone,
    }
    return {key: value for key, value in basics.items() if value}


# --- the rail ----------------------------------------------------------------------------------


def _provision_rail(ui: Ui, region) -> None:
    """Get the carousell.ai guest key. Quiet on success, and never fatal.

    A provisioning hiccup is a network problem, not an install problem: everything except the
    rail works without it, and the key can be obtained later. Saying so beats stopping.
    """
    from selly_agent.rail import provision

    if not region:
        return
    ui.step("Setting up carousell.ai")
    status = provision.ensure(region, api_base=config.load().carousell_ai_api_base)
    if status.get("status") == "ok":
        ui.say("ready — always enabled, with nothing to sign in to")
        return
    ui.warn(f"carousell.ai setup did not complete: {status.get('error')}")
    ui.note("re-run `selly-agent provision carousell-ai` when back online")


# --- marketplaces ---------------------------------------------------------------------------


def _connect_markets(ui: Ui, args, port: int, token: str, region) -> None:
    """Offer the marketplaces this seller could list on, and sign in to the ones they pick.

    This step *is* the opt-in to cross-listing: what they choose here becomes the setting the
    fan-out reads. carousell.ai is never in the list — it is the rail every listing goes on, with
    nothing to sign in to.
    """
    if args.skip_markets or not region:
        return
    available = market_adapters.publishable_markets(region)
    ui.step("Other marketplaces")
    if not available:
        ui.say("none available in this region yet — carousell.ai only")
        return

    ui.say("Listings can also be cross-posted to the marketplaces below. Sign-in happens in")
    ui.say("Selly's own Chrome window; it never signs in on your behalf. Skipping is fine —")
    ui.say("add them later with `selly-agent connect <name>`.")
    names = [marketplaces.display_name(market) for market in available]
    picked = [
        available[index]
        for index in ui.multiselect("Which marketplaces should Selly list on?", names, lead=False)
    ]
    if not picked:
        ui.say("carousell.ai only — change this any time from the /selly menu")
        return

    # The setting first: it is what the seller opted into, and it holds even if a sign-in is
    # interrupted — the fan-out re-checks the login every time it publishes anyway.
    if settings_cli.set_setting(port, token, "crosslist_markets", json.dumps(picked)) != 0:
        ui.warn("Those marketplaces could not be recorded — carousell.ai only for now.")
        return

    for market in picked:
        ui.say(f"opening {marketplaces.display_name(market)}…")
        connect_cli.market_flow(port, token, market, interactive=ui.interactive)


# --- Telegram ---------------------------------------------------------------------------------


def _offer_telegram(ui: Ui, args, port: int, token: str) -> None:
    """Offer the phone channel. Declining is a first-class answer — the agent runs without it,
    and everything it would push is queued and shown at the start of an attended session."""
    if args.skip_telegram:
        return
    ui.step("Telegram")
    if _channel_bound(port, token):
        ui.say("already connected")
        return

    ui.say("Telegram delivers buyer chats to your phone. Connecting takes about two minutes.")
    ui.say("Without it, anything needing you is queued for your next session in the terminal.")
    if not ui.interactive or not ui.confirm("Connect Telegram now?", default=True, lead=False):
        ui.say("skipped — connect later with `selly-agent connect telegram`")
        return

    code = connect_cli.bind_flow(port, token, interactive=ui.interactive)
    if code != 0:
        ui.say("not connected — resume later with `selly-agent connect telegram`")


def _channel_bound(port: int, token: str) -> bool:
    try:
        status, body = control.get(port, token, "/control/channel-status")
    except control.DaemonUnreachable:
        return False
    return status == 200 and bool(body.get("bound"))


# --- the attended session ----------------------------------------------------------------------


def _attended_workspace(ui: Ui) -> None:
    """Generate the Claude Code workspace `selly-agent chat` launches.

    Written here as well as by `chat` so the first session starts instantly, and so a seller who
    goes looking finds the directory already in place.
    """
    dest = pass_cli.attended_dir()
    ui.step("Terminal session")
    if pass_cli.harness_config(dest) != 0:
        ui.warn("The attended workspace could not be written.")
        ui.note("create it later with `selly-agent harness config --attended --dir <path>`")
        return
    ui.say("Talk to Selly in a terminal with:")
    ui.say("selly-agent chat")


# --- the last word ------------------------------------------------------------------------------


def _finish(ui: Ui, platform) -> None:
    ui.step("Checking the installation")
    for line in checks.render(healthcheck.run_checks(platform=platform)):
        ui.say(line)

    ui.step("Installed")
    ui.say("Selly is running.")
    ui.say("• Talk to Selly:    selly-agent chat   (`/selly` there changes settings)")
    ui.say("• Watch it work:    selly-agent logs --follow   (or --web)")
    ui.say("• Check status:     selly-agent daemon status")
    ui.say("• Update:           selly-agent update")


def _daemon_diagnostics() -> str:
    """What to look at when the worker did not come up — with the tail of its own stderr, since
    that is where the reason actually is and nobody finds that path on their own."""
    log_path = paths.logs_dir() / "agent.err.log"
    lines = [f"Its log is at {log_path}", "Run `selly-agent daemon status` for its view."]
    try:
        # errors="replace": this runs to explain a failure, and a log carrying one odd byte —
        # a subprocess's output in the machine's own code page — must not fail that explanation.
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = tail[-_LOG_TAIL_LINES:]
    except OSError:
        tail = []
    if tail:
        lines.append("Last lines:")
        lines.extend(f"  {line}" for line in tail)
    return "\n".join(lines)
