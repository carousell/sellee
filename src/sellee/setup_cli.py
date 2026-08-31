"""`sellee setup` — the deterministic installer, start to finish, in one terminal.

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
import sys
import time

from sellee import (
    __version__,
    channel,
    config,
    connect_cli,
    control,
    deployment,
    healthcheck,
    heartbeat,
    marketplaces,
    pass_cli,
    passes,
    paths,
    secrets,
    settings_cli,
    supervisor,
)
from sellee.browser import foreground
from sellee.browser import markets as market_adapters
from sellee.installer import checks, materialize, preflight
from sellee.installer import region as region_guess
from sellee.installer import update as update_mod
from sellee.installer.ui import Abort, Ui
from sellee.platform import get_platform

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

    # The container ships the machine half already done: the dependencies are in the image, the
    # layout is the image's own filesystem, and the worker was started by Docker before anyone
    # typed this. What is left is the half that is about the seller rather than the machine.
    if deployment.is_container():
        _intro_container(ui)
        if not _agreed_to_proceed(ui, nothing_written=False):
            return
    else:
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
    _browser_window(ui, port, token)
    _offer_channel(ui, args, port, token)
    _attended_workspace(ui)
    # Re-probed here rather than threaded through: the bind may have just happened inside
    # _offer_channel, and the closing next-step depends on where the seller can act.
    _finish(ui, platform, bound_to=_bound_channel(port, token))


# --- what this is, and what it will touch ---------------------------------------------------


def _intro(ui: Ui, platform) -> None:
    ui.banner(__version__)
    ui.say("")
    ui.say("Sellee is a marketplace agent: it lists items, answers buyers, and negotiates within")
    ui.say(f"limits you set. This installs version {__version__} on this computer.")
    ui.say("")
    ui.say("The installer will:")
    ui.say("  • check for Node, Chrome, and the claude CLI (installed and signed in)")
    ui.say("  • install this version, plus the `sellee` command")
    ui.say("  • register and start the background worker")
    ui.say("  • record the region and currency to price in")
    ui.say("  • optionally connect marketplaces and a chat channel (Telegram or Discord)")
    ui.say("")
    ui.say("Sellee will be installed into the following locations:")
    for line in materialize.layout_preview(platform=platform):
        ui.say(line)

    _note_agent_session(ui)


def _intro_container(ui: Ui) -> None:
    """The same account of what is about to happen, for an install that has no machine half.

    Nothing here is installed on the seller's computer, which is the whole reason this profile
    exists — so it is said plainly, along with the one thing that is not true of: the Chrome the
    agent drives is theirs, on their desktop, started by them.
    """
    ui.banner(__version__)
    ui.say("")
    ui.say("Sellee is a marketplace agent: it lists items, answers buyers, and negotiates within")
    ui.say(f"limits you set. Version {__version__} is already running in this container.")
    ui.say("")
    ui.say("This will:")
    ui.say("  • record the region and currency to price in")
    ui.say("  • set up carousell.ai")
    ui.say("  • optionally connect marketplaces and a chat channel (Telegram or Discord)")
    ui.say("  • write the workspace for the terminal session")
    ui.say("")
    ui.say("Everything it writes lands in the directory you mounted at /data, and nothing is")
    ui.say("installed on your computer. Chrome is the exception by design: it runs on your own")
    ui.say("desktop, you start it, and the agent drives it from here.")

    _note_agent_session(ui)


def _note_agent_session(ui: Ui) -> None:
    agent_var = preflight.agent_context()
    if agent_var and ui.interactive:
        # A TTY exists, but an agent is holding it. Questions asked here would be answered by
        # a model rather than the seller, so the run takes its defaults and says so.
        ui.say("")
        ui.warn(f"Running inside an agent session (${agent_var}) — questions take their defaults.")
        ui.interactive = False


def _agreed_to_proceed(ui: Ui, *, nothing_written: bool = True) -> bool:
    """The consent gate: nothing has been written before this, and nothing is until it passes.

    Every phase after this one either writes to disk or installs something, including the
    dependency gates (a `brew install`, a package download). So the whole account of what will
    happen is given first, and one answer covers it — the individually consequential steps
    (touching a shell rc, signing in, opening a marketplace) still ask again in their own words.

    The container has already written for itself by the time anyone runs this (the worker starts
    with the container), so there the promise would be false and is not made.
    """
    ui.say("")
    if nothing_written:
        ui.say("Nothing has been written yet.")
    if ui.confirm("Proceed with the installation?", default=True):
        return True
    ui.say("")
    ui.say(f"Cancelled — nothing was written. Re-run {_setup_command()} when you're ready.")
    return False


def _setup_command() -> str:
    """How to start this again. `./setup` is a checkout's front door and does not exist in the
    container, where the runtime is already established and only this verb remains."""
    return "sellee setup" if deployment.is_container() else "./setup"


# --- the gates ------------------------------------------------------------------------------


def _gates(ui: Ui, tree) -> None:
    ui.step("Checking this machine")
    cfg = config.load()

    _require(ui, checks.fail_open("install location", lambda: preflight.check_tree_location(tree)))
    _require(ui, checks.fail_open("python runtime", lambda: preflight.check_runtime(tree)))
    if sys.platform == "linux":
        # Before anything slow: with no user manager there is nothing to register the worker
        # with, and every later phase would be work thrown away.
        _require(ui, checks.fail_open("systemd", preflight.check_systemd_user))
    _gate_claude(ui, cfg)
    _gate_dependency(ui, "node", lambda: preflight.check_node(), package="node")
    _gate_dependency(
        ui,
        "chrome",
        lambda: preflight.check_chrome(cfg.chrome_bin),
        package="google-chrome",
        cask=True,
    )

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


def _gate_dependency(ui: Ui, name: str, probe, *, package: str, cask: bool = False) -> None:
    """A dependency we can offer to install, once. Homebrew itself is never bootstrapped —
    piping a remote installer into a shell is a trust decision the machine's owner owns."""
    check = _report(ui, checks.fail_open(name, probe))
    if not check.failed:
        return

    if sys.platform != "darwin":
        # Everywhere but macOS a system package needs `sudo`, and an installer must not wield
        # that on someone's behalf.
        raise Abort(f"{name}: {check.detail}", check.fix)

    brew = preflight.homebrew_path()
    if not brew:
        raise Abort(
            f"{name}: {check.detail}",
            f"{check.fix}\n(Homebrew isn't installed — get it from https://brew.sh, or install "
            f"{name} however you prefer, then re-run ./setup.)",
        )
    if not ui.confirm(f"Install {name} with Homebrew now?", default=True, lead=False):
        raise Abort(f"{name}: {check.detail}", check.fix)

    ui.say(f"Running `brew install {package}` — this can take a few minutes…")
    ok, detail = preflight.brew_install(package, cask=cask)
    if not ok:
        raise Abort(f"installing {name} failed: {detail}", check.fix)
    _require(ui, checks.fail_open(name, probe))


# --- the layout -----------------------------------------------------------------------------


def _install_layout(ui: Ui, args, platform, tree) -> None:
    # A re-run replaces the very directory a running daemon is executing out of, so it is stopped
    # first. Skipping this is not merely untidy: `launchctl bootstrap` is a no-op on a label that
    # is already loaded, so the old process would keep running — still ticking heartbeats, so the
    # wait below would pass — while setup reported the new version as up.
    ui.step(f"Installing Sellee {__version__}")
    if supervisor.gather_status(platform=platform).registered:
        ui.note("stopping the running worker so it picks up this version…")
        supervisor.stop(platform=platform)

    if args.dev:
        materialize.install_dev(tree)
        ui.say(f"dev mode: current → {tree}")
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
    """Make `sellee` findable, or say exactly why it is not.

    The uv/rustup convention: install into the user's own bin dir, then *offer* to touch the
    shell rc with an explicit way to decline — never silently edit dotfiles, and never silently
    leave a command that is not found.
    """
    if materialize.user_bin_on_path():
        return
    bin_dir = paths.user_bin_dir()
    export_line = materialize.RC_BLOCK_BODY
    ui.say("")
    ui.warn(f"{bin_dir} is not on your PATH, so `sellee` will not be found yet.")

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


def _explain_path_by_hand(ui: Ui, shell: str, bin_dir) -> None:
    """Say how to put the bin dir on the invoking shell's PATH, in that shell's own terms."""
    if shell == "fish":
        ui.say("Fish shell detected; its config is left unchanged. Run this once:")
        ui.say(materialize.FISH_COMMAND)
        ui.note(f"or in ~/.config/fish/config.fish:  {materialize.FISH_CONFIG_LINE}")
        return
    named = f"Shell: {shell}." if shell else "The shell could not be determined."
    ui.say(f"{named} Add {bin_dir} to PATH to run `sellee` by name.")


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
        ui.say("Run `sellee daemon start` when you want it.")
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
    from sellee.rail import provision

    if not region:
        return
    ui.step("Setting up carousell.ai")
    status = provision.ensure(region, api_base=config.load().carousell_ai_api_base)
    if status.get("status") == "ok":
        ui.say("ready — always enabled, with nothing to sign in to")
        return
    ui.warn(f"carousell.ai setup did not complete: {status.get('error')}")
    ui.note("re-run `sellee provision carousell-ai` when back online")


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
    ui.say("Sellee's own Chrome window; it never signs in on your behalf. Skipping is fine —")
    ui.say("add them later with `sellee connect <name>`.")
    names = [marketplaces.display_name(market) for market in available]
    picked = [
        available[index]
        for index in ui.multiselect("Which marketplaces should Sellee list on?", names, lead=False)
    ]
    if not picked:
        ui.say("carousell.ai only — change this any time from the /sellee menu")
        return

    # The setting first: it is what the seller opted into, and it holds even if a sign-in is
    # interrupted — the fan-out re-checks the login every time it publishes anyway.
    if settings_cli.set_setting(port, token, "connected_markets", json.dumps(picked)) != 0:
        ui.warn("Those marketplaces could not be recorded — carousell.ai only for now.")
        return

    for market in picked:
        ui.say(f"opening {marketplaces.display_name(market)}…")
        connect_cli.market_flow(port, token, market, interactive=ui.interactive)


# --- the browser window -------------------------------------------------------------------------


def _browser_window(ui: Ui, port: int, token: str) -> None:
    """Ask whether the agent's Chrome window should keep coming to the front.

    Asked after the marketplace sign-ins on purpose: the first window of an install must always
    come forward — a sign-in window the seller cannot find is a wall — and it does, because the
    `raise_browser` setting defaults to on and nothing has been asked yet. By now the seller has
    seen the behavior, so the question is concrete. Only a "no" is recorded: the default lives in
    code, and writing it back would list the setting as customized on the /sellee card.

    Skipped entirely where no window can be raised — an OS the raise doesn't support, or a container
    whose Chrome is on the seller's own desktop. Offering to turn off something that never happens
    teaches the seller something untrue about their install; the setting stays registered either
    way, so a machine that can raise still honors a value set elsewhere.
    """
    if deployment.is_container() or not foreground.is_supported():
        return
    ui.step("Browser window")
    ui.say("I do my browser work in a Chrome window of my own. When I open a page you need —")
    ui.say("like a marketplace sign-in — I bring that window to the front.")
    if not ui.confirm("Keep bringing my window to the front?", default=True, lead=False):
        if settings_cli.set_setting(port, token, "raise_browser", "false") != 0:
            ui.warn("That could not be recorded — I'll keep raising the window for now.")
    ui.note(
        "Change this any time: /sellee in chat, or `sellee settings set raise_browser true|false`."
    )


# --- the chat channel ---------------------------------------------------------------------------

# The channels a seller can pick, in menu order: id, then the name the menu shows for it.
_CHANNELS = (
    ("telegram", "Telegram"),
    ("discord", "Discord"),
)


def _offer_channel(ui: Ui, args, port: int, token: str) -> None:
    """Offer the chat channel as one pick-one menu.

    A menu rather than a yes/no per provider: only one channel binds (see store.arm_bind), so
    asking about Telegram first hid Discord behind that answer entirely — accept Telegram and
    Discord looked unavailable, decline it and the second question read as a re-ask. The seller
    has to see both to choose between them. Picking nothing stays a first-class answer: the agent
    runs without a channel and queues whatever needs the seller for their next terminal session.
    """
    choices = [c for c in _CHANNELS if not getattr(args, f"skip_{c[0]}", False)]
    if not choices:
        return
    ui.step("Chat channel")

    bound = _bound_channel(port, token)
    if bound is not None:
        ui.say(f"already connected: {channel.display_name(bound)}")
        ui.note("switch any time with `sellee connect <name>` — one channel at a time")
        return

    ui.say("You can interact with Sellee using various channels. Only one channel can be")
    ui.say("connected at a time, for now. Connecting takes about two minutes.")
    ui.say("Channels are optional, you can also interact with Sellee using the terminal.")
    index = ui.choose_optional("Connect now?", [label for _, label in choices], lead=False)
    if index is None:
        verbs = " or ".join(f"`sellee connect {name}`" for name, _ in choices)
        ui.say(f"skipped — connect later with {verbs}")
        return

    name = choices[index][0]
    if _bind_channel(port, token, name, interactive=ui.interactive) != 0:
        ui.say(f"not connected — resume later with `sellee connect {name}`")


def _bind_channel(port: int, token: str, name: str, *, interactive: bool) -> int:
    # Dispatched by name at call time rather than held in _CHANNELS, so the flows stay the ones
    # connect_cli exposes now (and the ones a test substitutes) instead of whatever was bound at
    # import.
    if name == "discord":
        return connect_cli.discord_bind_flow(port, token, interactive=interactive)
    return connect_cli.bind_flow(port, token, interactive=interactive)


def _bound_channel(port: int, token: str) -> str | None:
    """The provider a channel is bound to, or None. Unreachable or unnamed reads as None, which
    costs a re-offer rather than a wrong claim about what is connected."""
    try:
        status, body = control.get(port, token, "/control/channel-status")
    except control.DaemonUnreachable:
        return None
    if status != 200 or not body.get("bound"):
        return None
    return body.get("adapter")


# --- the attended session ----------------------------------------------------------------------


def _attended_workspace(ui: Ui) -> None:
    """Generate the Claude Code workspace `sellee chat` launches.

    Written here as well as by `chat` so the first session starts instantly, and so a seller who
    goes looking finds the directory already in place.
    """
    dest = pass_cli.attended_dir()
    ui.step("Terminal session")
    if pass_cli.harness_config(dest) != 0:
        ui.warn("The attended workspace could not be written.")
        ui.note("create it later with `sellee harness config --attended --dir <path>`")
        return
    ui.say("Talk to Sellee in a terminal with:")
    ui.say("sellee chat")


# --- the last word ------------------------------------------------------------------------------


def _finish(ui: Ui, platform, bound_to: str | None) -> None:
    ui.step("Checking the installation")
    for line in checks.render(healthcheck.run_checks(platform=platform)):
        ui.say(line)

    ui.step("Installed")
    ui.say("Sellee is running.")
    ui.say("")
    ui.say("Next: your first listing.")
    if bound_to:
        app = channel.display_name(bound_to)
        ui.say(f"Open {app} and send your Sellee bot a photo of something you want to sell —")
        ui.say("it checks the price with you before anything goes live.")
    else:
        ui.say("Run `sellee chat` and use /sell to list your first item.")
    ui.say("")
    container = deployment.is_container()
    if container:
        # The CLI lives in here, not on the seller's PATH. How they get back in is their
        # container runtime's business — the same way they reached this prompt.
        ui.say("Run these inside the container, the way you ran this:")
    ui.say("• Talk to Sellee:    sellee chat   (`/sellee` there changes settings)")
    ui.say("• Watch it work:    sellee logs --follow" + ("" if container else "   (or --web)"))
    ui.say("• Check status:     sellee daemon status")
    if container:
        ui.say(f"• Update:           {update_mod.CONTAINER_UPDATE_HOW}")
    else:
        ui.say("• Update:           sellee update")


def _daemon_diagnostics() -> str:
    """What to look at when the worker did not come up — with the tail of its own stderr, since
    that is where the reason actually is and nobody finds that path on their own."""
    log_path = paths.logs_dir() / "agent.err.log"
    if deployment.is_container():
        # The worker's stderr goes to the container's own output rather than to a file, so the
        # reason is wherever that runtime collects logs.
        return "The worker logs to the container's output — read it with your container runtime."
    lines = [f"Its log is at {log_path}", "Run `sellee daemon status` for its view."]
    try:
        tail = log_path.read_text().splitlines()[-_LOG_TAIL_LINES:]
    except OSError:
        tail = []
    if tail:
        lines.append("Last lines:")
        lines.extend(f"  {line}" for line in tail)
    return "\n".join(lines)
