"""The daemon process: lock, migrate, observe, schedule — idle but fully observable.

Startup order is deliberate: acquire the instance lock (a live duplicate exits 0 before doing
anything), ensure dirs, run startup migrations against untouched DBs, then open the event bus
and start the scheduler. Migrations run before the bus exists (the events DB is one of the
things being migrated), so migration.applied events are emitted from the returned list once the
bus is up. SIGTERM/SIGINT request a stop; the loop drains, emits daemon.stop, clears the lock
body, and exits 0.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import signal
import subprocess
import sys
import threading
import time

from sellee import (
    __version__,
    clock,
    config,
    crosslist,
    deployment,
    heartbeat,
    intent_sweep,
    lock,
    migrations,
    passes,
    paths,
    retention,
    secrets,
    settings,
)
from sellee.browser import chrome, inbox
from sellee.browser import client as browser_client
from sellee.browser import connect as browser_connect
from sellee.browser import sink as browser_sink
from sellee.browser import survey as browser_survey
from sellee.browser import window as browser_window
from sellee.channel import outbound
from sellee.channel.discord import provider as discord_provider
from sellee.channel.manager import ChannelManager
from sellee.channel.telegram import provider as telegram_provider
from sellee.db import Database
from sellee.events import EventBus, EventStore
from sellee.http_server import HttpServer
from sellee.installer import update
from sellee.rail.client import RailClient, RailUnprovisioned
from sellee.scheduler import Scheduler, Task
from sellee.store import ScopedStore, Store
from sellee.tools.registry import ToolContext

log = logging.getLogger(__name__)

# How often the pass lane checks the queue, and the stray reaper scans. The lane is single-flight,
# so a short interval only affects pickup latency, never concurrency.
_PASS_LANE_INTERVAL_SEC = 2.0
_STRAY_REAPER_INTERVAL_SEC = 60.0
_INTENT_SWEEP_INTERVAL_SEC = 120.0
# The reply lane only queries durable rows, so it can run often; what paces buyer replies is the
# pacing gate inside the send, not how often this looks.
_REPLY_LANE_INTERVAL_SEC = 10.0
# The proposal TTL is a day; an hourly sweep gives at most an hour's slack past it. The doors also
# enforce the TTL inline when a stale id is tapped, so this only cleans up the never-answered ones.
_SETTINGS_EXPIRY_INTERVAL_SEC = 3600.0
# The fan-out lane only reads durable rows and queues at most one publish per tick, and a browser
# publish takes minutes — so this is about how soon a seller hears their listing went up, not about
# throughput.
_CROSSLIST_LANE_INTERVAL_SEC = 30.0
# The connect lane reads one indexed row and does nothing the rest of the time, so this is purely
# how long a seller waits after tapping Sign in on desktop before Chrome starts moving. They are
# watching the chat when they tap, so it is short.
_CONNECT_LANE_INTERVAL_SEC = 2.0
# The survey lane reads indexed rows and does nothing the rest of the time. A minute is about how
# long a seller waits to be asked after signing in, and it also paces adoption — one listing per
# tick, each a page read and a set of photographs.
_SURVEY_LANE_INTERVAL_SEC = 60.0
# A cold `npx` fetch of the browser server and its dependencies is a download, so minutes. This is
# off the hot path entirely, so it only has to be longer than a slow connection needs.
_BROWSER_WARM_TIMEOUT_SEC = 600.0

# Said whenever acquiring the browser had to start Chrome. Deliberately names no flow: any actor
# that needs the browser may be the one that opens the window, and the seller only needs to know
# it was us.
CHROME_STARTED_NOTICE = (
    "Starting the agent's Chrome window — that's expected, you can leave it running."
)

# Said when Chrome was already running with no window of its own and we had to open one. It is a
# different sentence from the one above because it answers a different question: not "why did Chrome
# start" but "why did a window I closed come back". Closing the window does not quit Chrome on
# macOS, and the browser server opens a tab for any page tool when it has none — so the window
# returns on the next read whatever we do, and the only honest thing is to say so and ask for a
# minimise instead.
CHROME_WINDOW_REOPENED_NOTICE = (
    "I reopened my Chrome window — I need it open to read your marketplace inboxes. Minimise it if "
    "it's in your way, but closing it doesn't stop me: Chrome keeps running without a window, and "
    "I have to open a new one the next time I look."
)

# How often the browser server may be thrown away for a lost Chrome connection before we stop and
# say so, and over what window. One wedge is a fluke — a laptop that slept, a network that dropped
# mid-navigation, which is exactly what preceded the 28-hour outage on 2026-08-27. Three inside an
# hour is not something a fourth process is going to fix, and respawning node forever is how a bug
# becomes invisible. The window is what makes the giving-up temporary: the daemon starts trying
# again by itself rather than needing a restart.
BROWSER_RECYCLE_MAX = 3
BROWSER_RECYCLE_WINDOW_SEC = 3600.0
# The shortest gap between two recycles. Without it the fastest caller drains the budget for
# everyone: the connect lane acquires the browser every two seconds while a sign-in row is pending,
# and a pending row is precisely what the can't-read notice produces — so three of its ticks would
# spend the hour's allowance in six seconds and leave the read lane, which ticks every five minutes,
# reporting the browser unavailable for the remaining fifty-nine.
BROWSER_RECYCLE_COOLDOWN_SEC = 120.0
# A server is replaced at this age even when nothing has gone wrong with it. Not because age was the
# defect — the 2026-08-27 wedge was a network event, and the process was idle at 25 file descriptors
# — but because a long-lived process holding a socket to a browser is a thing that rots, and the
# cost here is one graceful swap a day: the outgoing client closes its own tab and the incoming one
# opens one, so the seller sees nothing.
BROWSER_RECYCLE_AGE_SEC = 12 * 3600.0
RECYCLE_EXHAUSTED_REASON = (
    "my connection to Chrome keeps dying — I have replaced the browser server "
    f"{BROWSER_RECYCLE_MAX} times within the hour and it keeps losing Chrome"
)


def ensure_chrome(cfg, store, bus, should_stop=None) -> int:
    """Make Chrome answer on its CDP port and return that port, or raise `BrowserUnavailable`
    with the by-hand command.

    Acquiring the browser means ensuring it runs: this is the one place that rule lives, called on
    every acquisition because Chrome can be closed at any moment after the client is built. A launch
    queues the seller notice before anything drives the window, so it never appears unexplained.

    The port comes back rather than going in, because unpinned it is Chrome's to choose and is only
    known once Chrome is up.

    Where the browser is the seller's own, on a machine this process only reaches over a port, the
    acquisition is the probe and nothing else — so no window is ever started, and a closed one is
    reported with the instruction for starting it there. That side has to be given a number: the
    profile Chrome announces its port into is on the other machine, so the port is an agreement
    between two processes that share no filesystem, and the only thing to do is resolve it here.
    """
    may_launch = deployment.manages_chrome()
    asked = cfg.chrome_cdp_port if may_launch else chrome.resolve_port(cfg.chrome_cdp_port)
    state, port = chrome.ensure_running(
        asked,
        chrome_bin=cfg.chrome_bin,
        may_launch=may_launch,
        should_stop=should_stop,
    )
    if state == chrome.UNAVAILABLE or port is None:
        hint = (
            chrome.bring_up_hint(asked, chrome_bin=cfg.chrome_bin)
            if may_launch
            else chrome.container_bring_up_hint(asked)
        )
        raise browser_client.BrowserUnavailable(hint)
    if state == chrome.LAUNCHED:
        store.queue_notice(CHROME_STARTED_NOTICE)
        bus.publish("browser.chrome_launched", {"port": port})
    return port


def warm_browser_server(cfg, *, once: bool) -> threading.Thread | None:
    """Fetch the pinned browser server into the npx cache in the background, at startup.

    Two things leave that cache without it, both after the installer warmed it: an update that
    bumped the pinned version (an update never re-runs setup's gates), and npm pruning a cache we
    do not own. Either way the next spawn becomes a download, which can outrun the client's startup
    timeout — turning "the first browser action is slower" into "the first browser action fails".

    A thread, so nothing waits on it, and its failures are only logged: a machine that is offline
    right now still starts, and still recovers the moment it can fetch.

    Skipped under an override — we do not know a foreign command's warming semantics — and in a
    single-tick run, which is a smoke check that must not reach the network.
    """
    if once or cfg.playwright_mcp_cmd:
        return None

    def warm() -> None:
        argv = [browser_client.SERVER_BINARY, "--yes", browser_client.PINNED_MCP_SPEC, "--version"]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=_BROWSER_WARM_TIMEOUT_SEC,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not warm %s: %s", browser_client.PINNED_MCP_SPEC, exc)
            return
        if proc.returncode == 0:
            log.debug("%s is in the npx cache", browser_client.PINNED_MCP_SPEC)
        else:
            log.warning(
                "could not warm %s (exit %s): %s",
                browser_client.PINNED_MCP_SPEC,
                proc.returncode,
                (proc.stderr or "").strip()[-200:],
            )

    thread = threading.Thread(target=warm, name="browser-warm", daemon=True)
    thread.start()
    return thread


# The phrase that distinguishes the diagnosis from the age ceiling, in one place so the two callers
# that branch on it cannot drift apart from the sentence itself.
DETACHED_MARKER = "lost its connection to Chrome"
DETACHED_REASON = f"the browser server {DETACHED_MARKER}"
AGE_REASON = "the browser server reached its age ceiling"


def _recycle_reason(client, chrome_up, *, now) -> str | None:
    """Why this cached client should be thrown away, or None to keep it.

    Two reasons, and only the first is a diagnosis. A run of tool failures says the server is
    answering us and failing everything; Chrome answering its own probe says the browser it was
    meant to be driving is fine. Neither half is enough on its own — a marketplace that redesigned
    its DOM also fails every call, and respawning node for that would hide a broken adapter forever
    — so the recycle is what both together mean: the server has lost Chrome.

    The client cannot make this call itself. It has no way to ask about Chrome, and the obvious way
    to ask its server (listing tabs) is not read-only: that handler opens a tab when there is none,
    so diagnosing the fault would pop a window on a Chrome whose window the seller had closed.

    `chrome_up` is a callable so the steady state costs an integer compare and nothing else. Asking
    Chrome is another loopback round trip on a path every send and every lane tick goes through, and
    the answer is only interesting once the streak says something is already wrong.
    """
    if client.failing_streak() >= browser_client.RECYCLE_AFTER_FAILURES and chrome_up():
        return DETACHED_REASON
    if client.age_sec(now=now) >= BROWSER_RECYCLE_AGE_SEC:
        return AGE_REASON
    return None


def recycle_browser_client(bus, holder: dict, reason: str, *, now) -> None:
    """Throw away a browser server so the next acquisition builds a fresh one.

    Rebuilt by the factory rather than restarted in place, because the CDP endpoint is baked into
    the argv and a Chrome that came back on a different port is one of the ways a server ends up
    detached — restarting with the old command would dial a dead port and spend the whole allowance
    in seconds.

    Raises `BrowserUnavailable` once replacing it has stopped helping, which every caller already
    handles and which reaches the seller through the notice that already exists for a browser that
    cannot be driven at all.

    This is not the retry the client forbids. Nothing here repeats an action against a marketplace:
    no page is navigated, no key is pressed, no request is made that a site could observe. It
    replaces a subprocess of ours, and the work that failed is re-derived from durable rows by
    whichever lane comes back next tick.
    """
    recycles = tuple(
        stamp for stamp in holder.get("recycles", ()) if now() - stamp < BROWSER_RECYCLE_WINDOW_SEC
    )
    holder["recycles"] = recycles
    if recycles and now() - recycles[-1] < BROWSER_RECYCLE_COOLDOWN_SEC:
        return  # too soon to be a different answer; keep using what we have
    if len(recycles) >= BROWSER_RECYCLE_MAX:
        raise browser_client.BrowserUnavailable(RECYCLE_EXHAUSTED_REASON)
    stale = holder.pop("client", None)
    holder.pop("command", None)
    holder["recycles"] = (*recycles, now())
    bus.publish(
        "browser.recycled",
        {"reason": reason, "recycles": len(recycles) + 1, "window_sec": BROWSER_RECYCLE_WINDOW_SEC},
    )
    if stale is not None:
        # Published first: closing takes the client's lock, so it waits behind an operation still in
        # flight, and a detached server is given no chance to be polite about its tab.
        stale.close(graceful=DETACHED_MARKER not in reason)


def make_browser_factory(cfg, store, bus, holder: dict, should_stop=None, now=time.monotonic):
    """The daemon's one browser acquisition path: every actor that needs the browser — the read
    lane, the reply send, the selector probe, the fan-out — goes through the factory this returns.

    Every precondition is re-checked on every acquisition, not just at construction: the client is
    cached (in `holder`, so the daemon's shutdown can close it), but Chrome can be closed at any
    point after — and Node is checked first, so a machine that cannot drive a browser never has one
    opened for it. That check only ever looks at the binary, which is why it can still come first
    now that the CDP endpoint is not known until Chrome is up.

    A cached client is only reusable while it dials the right endpoint. An unpinned Chrome that was
    closed and started again comes back on a different port, so the command is compared and a client
    holding the old one is replaced rather than left to fail every call.

    **And only while it can still reach Chrome.** A server that answers us and fails everything it
    is asked was, until this, immortal: `BrowserClient._start` respawns a dead process and there is
    no such thing as a dead-enough live one. That is what let a wedged server report a market blind
    126 times over 28 hours while Chrome sat there signed in.

    Recycling is deliberately placed here rather than in a lane. The factory is the one boundary
    that is always *between* operations — every caller resolves it before entering
    `client.exclusive()` — which is what keeps a recycle from ever landing inside a send, past the
    line where the buyer may already have the message. That ordering is the guarantee; the client's
    lock is not, because it is re-entrant and would let a caller that acquired the browser from
    inside its own bracket walk straight through. Any new caller must keep the order.

    Watch mode is read here for the same reason the preconditions are: it is the seller's, it
    changes at runtime, and every actor that drives the browser comes through this door — so one
    read per acquisition is what makes a toggle in chat reach the read lane, the send and the
    attended tools alike, from their next call on.
    """

    def browser_factory():
        browser_client.ensure_available(cfg.playwright_mcp_cmd or [browser_client.SERVER_BINARY])
        port = ensure_chrome(cfg, store, bus, should_stop)
        _notice_window_reopening(store, bus, holder, port)
        command = cfg.playwright_mcp_cmd or browser_client.default_command(
            browser_client.cdp_endpoint(port)
        )
        client = holder.get("client")
        if client is not None and holder.get("command") != command:
            # A moved port is not a fault of the server's, so it is never charged against the
            # recycle allowance — otherwise a Chrome the seller restarts a few times would spend it.
            client.close()
            holder.pop("client", None)
            holder.pop("command", None)
            client = None
        if client is not None:
            reason = _recycle_reason(client, lambda: chrome.is_ready(port), now=now)
            # Never while a pass is driving: a publish holds the tab across many calls without the
            # client's own mutex, so replacing its server mid-flow abandons a half-filled listing
            # form and starts the whole thing again from nothing. Asked only once there is a reason
            # to act, so the steady state does not pay for a query about passes on every send.
            if reason is not None and not inbox.browser_pass_running(store):
                if DETACHED_MARKER in reason:
                    # Marked before the recycle is attempted, not after. If the cooldown declines to
                    # replace it we hand the same client back, and it has to fail fast and name the
                    # right cause — otherwise the lane spends the tick timing out and then blames
                    # the seller's Chrome for it.
                    client.mark_detached(reason)
                recycle_browser_client(bus, holder, reason, now=now)
                client = holder.get("client")
        if client is None:
            client = browser_client.BrowserClient(command=command)
            holder["client"] = client
            holder["command"] = command
        client.set_follow(bool(settings.get(store, browser_window.WATCH_SETTING)))
        return client

    return browser_factory


def _notice_window_reopening(store, bus, holder: dict, port: int) -> None:
    """Tell the seller once when Chrome is running with no window and we are about to open one.

    Chrome's own `/json/list`, not a browser tool: this has to be answerable *without* creating the
    thing it is counting, and asking the browser server to list tabs opens one when there are none.

    Latched, and re-armed as soon as a page exists again, so the seller hears this each time they
    close the window and never twice for the same one.
    """
    pages = chrome.page_targets(port)
    if pages is None:
        return  # could not tell; say nothing rather than guess
    if pages > 0:
        holder.pop("said_window_reopened", None)
        return
    if holder.get("said_window_reopened"):
        return
    holder["said_window_reopened"] = True
    store.queue_notice(CHROME_WINDOW_REOPENED_NOTICE)
    bus.publish("browser.window_reopened", {"port": port})


def _is_loopback_bind(host: str) -> bool:
    """Whether binding to `host` keeps the socket on this machine."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _warn_if_exposed(host: str) -> None:
    if _is_loopback_bind(host):
        return
    log.warning(
        "The control server is bound to %s, not loopback. The daemon is accessible remotely. "
        "This can be a security risk.",
        host,
    )


def _setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


def _install_signal_handlers(stop: threading.Event) -> None:
    def handler(signum, _frame):
        log.info("received signal %s — stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def run_daemon(*, once: bool) -> int:
    cfg = config.load()
    _setup_logging(cfg.log_level)

    # Install handlers before doing any real work, so a signal during startup is captured in the
    # stop flag and the loop exits cleanly rather than dying to the default disposition.
    stop = threading.Event()
    if not once:
        _install_signal_handlers(stop)

    lock_result = lock.acquire(paths.lock_path())
    if not lock_result.acquired:
        # A live duplicate holds the lock. INFO + exit 0 so KeepAlive won't respawn us.
        log.info(
            "sellee daemon already running (pid=%s, alive=%s) — exiting",
            lock_result.holder_pid,
            lock_result.holder_alive,
        )
        return 0
    _lock_fd = lock_result.fd  # noqa: F841 — held for the process lifetime (OS frees on exit)
    if lock_result.reclaimed:
        log.info("reclaimed a stale instance lock (previous holder was dead) — starting fresh")

    paths.ensure_runtime_dirs()
    data_db = Database(paths.sellee_db())
    events_db = Database(paths.events_db())

    applied = migrations.run_startup_migrations(
        data_db=data_db,
        events_db=events_db,
        backups_dir=paths.backups_dir(),
        backups_keep=cfg.backups_keep,
    )

    bus = EventBus(EventStore(events_db))
    bus.publish("daemon.start", {"pid": os.getpid(), "version": __version__, "once": once})
    for entry in applied:
        bus.publish(
            "migration.applied",
            {"db": entry.db, "version": entry.version, "name": entry.name},
        )

    store = Store(data_db)
    started_ts = time.time()

    # In a container the clock is whatever the image was started with, not the seller's own, and
    # every quiet-hours decision below is made against the local wall clock. A silent eight-hour
    # offset would move the whole of "don't do this at night" onto the wrong eight hours.
    if deployment.is_container():
        clock.check_timezone(store=store, bus=bus)

    attended_token = secrets.ensure_mcp_token()

    # Core needs-me wiring — always on, provider-independent (the queue works with no channel
    # bound). Push every new escalation to a queued notice; subscribed before work starts so
    # nothing is missed (a miss still surfaces via catchup). The channel-pass inbox fold is NOT
    # a subscriber — it runs as a scheduler lane off durable rows (registered below).
    bus.subscribe(outbound.escalation_notifier(store))

    # Turning watch mode on brings the agent's window forward once, so the seller knows which
    # Chrome to look at. Subscribed rather than done at the door because there are four doors — the
    # card button, /watch, the shell, and an approved proposal — and every one of them publishes
    # this event.
    bus.subscribe(browser_window.watch_raiser(cfg))

    def rail_factory():
        key = secrets.read_carousell_ai_api_key()
        if not key:
            raise RailUnprovisioned("carousell.ai is not provisioned")
        return RailClient(
            api_base=cfg.carousell_ai_api_base,
            api_key=key,
            web_base_url=cfg.carousell_ai_web_base_url,
        )

    # One browser client for the whole daemon: one Chrome, one tab, one mutex. Built lazily by the
    # factory so a machine with no Node still starts, with its browser lanes reporting unavailable
    # instead of the daemon failing at boot.
    browser_holder: dict = {}
    browser_factory = make_browser_factory(cfg, store, bus, browser_holder, stop.is_set)
    warm_browser_server(cfg, once=once)

    def reply_sink_factory():
        """The marketplace send, built when a send actually needs it — never at context build, so
        only a tool that intends to send acquires the browser (and starts Chrome, if that is all
        that is missing). The sink writes through the unscoped store: it stamps the intent it was
        handed, which the tool has already checked against the session's scope."""
        return browser_sink.BrowserReplySink(
            client=browser_factory(),
            store=store,
            bus=bus,
            region=inbox.seller_region(store),
            on_drive=lambda: browser_window.raise_if_watching(cfg, store),
        )

    def context_factory(session):
        # The store a handler sees is scoped to the session: attended (scope None) is a
        # transparent pass-through; a headless pass is held to its spawn-time entity scope at
        # every row load, so a thread never leaves the store without passing the scope check.
        return ToolContext(
            session=session,
            store=ScopedStore(store, getattr(session, "scope", None)),
            bus=bus,
            config=cfg,
            rail_factory=rail_factory,
            browser_factory=browser_factory,
            reply_sink=reply_sink_factory,
            started_ts=started_ts,
        )

    scheduler = Scheduler(
        bus,
        tick_interval_sec=cfg.tick_interval_sec,
        on_tick=lambda: heartbeat.write(paths.heartbeat_path()),
        stop_event=stop,
    )

    # Channel providers run only when registered: at boot for those already configured, and at
    # runtime when `connect` brings one up (the connect control route calls channels.register).
    # A daemon with no channel set up starts no channel thread at all. Skipped in --once (the test
    # seam): no provider spins, and the connect route is a no-op without a manager.
    channels = (
        None
        if once
        else ChannelManager(
            providers={"telegram": telegram_provider, "discord": discord_provider},
            bus=bus,
            store=store,
            config=cfg,
            scheduler=scheduler,
        )
    )

    # Loopback everywhere except a container, whose image sets the wildcard so a published port has
    # something to reach; compose keeps the host exposure on loopback either way. Note the Host
    # guard does not make a wider bind safe — the client chooses that header, so
    # `curl -H 'Host: 127.0.0.1'` satisfies it from anywhere. It defends against DNS rebinding, not
    # against who can open the socket, which is why a non-loopback bind warns below.
    bind_host = os.environ.get("SELLEE_BIND_HOST", "").strip() or "127.0.0.1"
    try:
        http = HttpServer(
            port=cfg.http_port,
            host=bind_host,
            bus=bus,
            store=store,
            events_db_path=events_db.path,
            context_factory=context_factory,
            attended_token=attended_token,
            config=cfg,
            channels=channels,
        )
    except OSError as exc:
        # A fixed config port; a bind failure (port in use, etc.) is fatal — fail loud so
        # launchd's throttle paces respawns rather than running half-initialized.
        log.error("http server bind failed on %s:%s: %s", bind_host, cfg.http_port, exc)
        lock.clear_holder(paths.lock_path())
        data_db.close()
        events_db.close()
        return 3
    http.start()
    _warn_if_exposed(bind_host)

    pass_deps = passes.PassDeps(
        bus=bus,
        store=store,
        config=cfg,
        auth=http.auth,
        http_endpoint=f"http://127.0.0.1:{http.port}/mcp",
        stop_event=stop,
        argv_builder=passes.default_argv_builder(cfg),
    )

    scheduler.register(
        Task(
            name="retention",
            interval_sec=float(retention.SECONDS_PER_DAY),
            func=lambda: retention.run_retention(
                bus=bus,
                retention_days=cfg.retention_days,
                routine_events_retention_hours=cfg.routine_events_retention_hours,
                backups_dir=paths.backups_dir(),
                backups_keep=cfg.backups_keep,
                logs_dir=paths.logs_dir(),
            ),
        )
    )
    scheduler.register(
        Task(
            name="pass_lane",
            interval_sec=_PASS_LANE_INTERVAL_SEC,
            func=lambda: passes.pass_lane(pass_deps),
        )
    )
    scheduler.register(
        Task(
            name="stray_reaper",
            interval_sec=_STRAY_REAPER_INTERVAL_SEC,
            func=lambda: passes.stray_reaper(pass_deps),
        )
    )
    scheduler.register(
        Task(
            name="stale_intent_sweep",
            interval_sec=_INTENT_SWEEP_INTERVAL_SEC,
            func=lambda: intent_sweep.run_stale_intent_sweep(bus=bus, store=store),
        )
    )
    # Expire never-answered settings proposals (always on, channel-independent — a proposal is
    # durable the moment it's written; catchup surfaces it meanwhile).
    scheduler.register(
        Task(
            name="settings_expiry_sweep",
            interval_sec=_SETTINGS_EXPIRY_INTERVAL_SEC,
            func=lambda: settings.expire_stale_proposals(store, bus),
        )
    )
    # Read the browser marketplaces' inboxes into durable rows. Deterministic and token-free, which
    # is what lets the reply loop above it run without any browser access of its own.
    inbox_deps = inbox.InboxDeps(
        store=store,
        bus=bus,
        config=cfg,
        browser_factory=browser_factory,
    )
    scheduler.register(
        Task(
            name="inbox_read",
            interval_sec=float(cfg.inbox_read_interval_sec),
            func=lambda: inbox.inbox_lane(inbox_deps),
        )
    )
    # Sign the seller in to a marketplace when they ask from chat. The tap itself lands on the
    # provider's receive loop, which must keep answering messages, so it only writes a row — this
    # is what actually drives Chrome, and what tells them how it went.
    connect_deps = browser_connect.ConnectDeps(
        store=store,
        bus=bus,
        config=cfg,
        browser_factory=browser_factory,
    )
    scheduler.register(
        Task(
            name="market_connect",
            interval_sec=_CONNECT_LANE_INTERVAL_SEC,
            func=lambda: browser_connect.connect_lane(connect_deps),
        )
    )
    # Read the seller's existing listings once, ask, and turn a yes into items.
    survey_deps = browser_survey.SurveyDeps(
        store=store,
        bus=bus,
        config=cfg,
        browser_factory=browser_factory,
    )
    scheduler.register(
        Task(
            name="market_survey",
            interval_sec=_SURVEY_LANE_INTERVAL_SEC,
            func=lambda: browser_survey.survey_lane(survey_deps),
        )
    )
    # Answer the buyers who are waiting. Driven off durable rows rather than off the read lane, so a
    # crash between reading a message and answering it still gets answered.
    scheduler.register(
        Task(
            name="reply_lane",
            interval_sec=_REPLY_LANE_INTERVAL_SEC,
            func=lambda: inbox.reply_lane(store=store, bus=bus, config=cfg),
        )
    )
    # List what is on carousell.ai everywhere else the seller sells, and report each outcome. Driven
    # off stored rows, so rail-first is a precondition rather than a step a recipe could skip.
    crosslist_deps = crosslist.CrosslistDeps(
        store=store, bus=bus, config=cfg, browser_factory=browser_factory, rail_factory=rail_factory
    )
    scheduler.register(
        Task(
            name="crosslist_lane",
            interval_sec=_CROSSLIST_LANE_INTERVAL_SEC,
            func=lambda: crosslist.crosslist_lane(crosslist_deps),
        )
    )
    # Look for a new release and say so once. Nothing installs itself — an update replaces the
    # code this process is running, so it stays something a person starts.
    #
    # Not in a single-tick run: that mode exists to prove the loop works, and every task's first
    # tick is immediate, so registering this would put an outbound request to the release host in
    # the path of a smoke check — slow where there is no network, and reaching a third party from
    # a test suite. Its own tests cover it against a local server.
    seen_versions: set = set()
    if not once:
        scheduler.register(
            Task(
                name="update_probe",
                interval_sec=float(cfg.update_check_interval_sec),
                func=lambda: update.update_probe(
                    store=store, bus=bus, config_obj=cfg, seen=seen_versions
                ),
            )
        )
    # Fold settled channel passes' claimed inbox rows from durable state (not a pass.end
    # subscriber), so a crash at any point — including a stale-swept pass — still folds the
    # seller's messages and queues the failure notice. Always on, channel-provider-independent.
    scheduler.register(
        Task(
            name="inbox_fold",
            interval_sec=outbound.INBOX_FOLD_INTERVAL_SEC,
            func=lambda: outbound.fold_settled_passes(store=store),
        )
    )
    # One first-listing nudge, ever, for a seller who connected a day ago and never listed.
    # Store-only (no network), so unlike update_probe it stays registered in a single-tick run.
    scheduler.register(
        Task(
            name="first_listing_nudge",
            interval_sec=outbound.FIRST_LISTING_NUDGE_INTERVAL_SEC,
            func=lambda: outbound.first_listing_nudge(store=store),
        )
    )
    # Say when a buyer has gone unanswered. The reply lane holds a pass it knows would be refused,
    # which is right — but held reads exactly like handled unless something says otherwise, and a
    # blocked reply records no intent, no transcript row and no error to notice it by.
    scheduler.register(
        Task(
            name="buyer_waiting_notice",
            interval_sec=outbound.BUYER_WAITING_INTERVAL_SEC,
            func=lambda: outbound.buyer_waiting_notice(store=store),
        )
    )

    # Start the providers already configured (their poller + delivery lanes); a fresh `connect`
    # starts one later at runtime. The delivery/typing scheduler tasks are registered inside the
    # provider's start, so they exist only while a provider runs.
    if channels is not None:
        channels.register_configured()

    try:
        if once:
            scheduler.run_once()
        else:
            scheduler.run()
    finally:
        if channels is not None:
            channels.shutdown_all()
        scheduler.shutdown()
        # After the lanes have stopped, so nothing is mid-call: this closes the tab we opened and
        # ends the MCP process, leaving the seller's warm Chrome as we found it.
        if browser_holder.get("client") is not None:
            browser_holder["client"].close()
        http.stop()
        bus.publish("daemon.stop", {"pid": os.getpid()})
        lock.clear_holder(paths.lock_path())
        data_db.close()
        events_db.close()
    return 0
