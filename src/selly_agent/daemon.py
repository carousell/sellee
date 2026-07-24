"""The daemon process: lock, migrate, observe, schedule — idle but fully observable.

Startup order is deliberate: acquire the instance lock (a live duplicate exits 0 before doing
anything), ensure dirs, run startup migrations against untouched DBs, then open the event bus
and start the scheduler. Migrations run before the bus exists (the events DB is one of the
things being migrated), so migration.applied events are emitted from the returned list once the
bus is up. SIGTERM/SIGINT request a stop; the loop drains, emits daemon.stop, clears the lock
body, and exits 0.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

from selly_agent import (
    __version__,
    config,
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
from selly_agent.channel import outbound
from selly_agent.channel.manager import ChannelManager
from selly_agent.channel.telegram import provider as telegram_provider
from selly_agent.db import Database
from selly_agent.events import EventBus, EventStore
from selly_agent.http_server import HttpServer
from selly_agent.rail.client import RailClient, RailUnprovisioned
from selly_agent.scheduler import Scheduler, Task
from selly_agent.store import ScopedStore, Store
from selly_agent.tools.registry import ToolContext

log = logging.getLogger(__name__)

# How often the pass lane checks the queue, and the stray reaper scans. The lane is single-flight,
# so a short interval only affects pickup latency, never concurrency.
_PASS_LANE_INTERVAL_SEC = 2.0
_STRAY_REAPER_INTERVAL_SEC = 60.0
_INTENT_SWEEP_INTERVAL_SEC = 120.0
# The proposal TTL is a day; an hourly sweep gives at most an hour's slack past it. The doors also
# enforce the TTL inline when a stale id is tapped, so this only cleans up the never-answered ones.
_SETTINGS_EXPIRY_INTERVAL_SEC = 3600.0


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
            "selly-agent daemon already running (pid=%s, alive=%s) — exiting",
            lock_result.holder_pid,
            lock_result.holder_alive,
        )
        return 0
    _lock_fd = lock_result.fd  # noqa: F841 — held for the process lifetime (OS frees on exit)
    if lock_result.reclaimed:
        log.info("reclaimed a stale instance lock (previous holder was dead) — starting fresh")

    paths.ensure_runtime_dirs()
    data_db = Database(paths.selly_db())
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
    attended_token = secrets.ensure_mcp_token()

    # Core needs-me wiring — always on, provider-independent (the queue works with no channel
    # bound). Push every new escalation to a queued notice; subscribed before work starts so
    # nothing is missed (a miss still surfaces via catchup). The channel-pass inbox fold is NOT
    # a subscriber — it runs as a scheduler lane off durable rows (registered below).
    bus.subscribe(outbound.escalation_notifier(store))

    def rail_factory():
        key = secrets.read_carousell_ai_api_key()
        if not key:
            raise RailUnprovisioned("carousell.ai is not provisioned")
        return RailClient(
            api_base=cfg.carousell_ai_api_base,
            api_key=key,
            web_base_url=cfg.carousell_ai_web_base_url,
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
            providers={"telegram": telegram_provider},
            bus=bus,
            store=store,
            config=cfg,
            scheduler=scheduler,
        )
    )

    try:
        http = HttpServer(
            port=cfg.http_port,
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
        log.error("http server bind failed on 127.0.0.1:%s: %s", cfg.http_port, exc)
        lock.clear_holder(paths.lock_path())
        data_db.close()
        events_db.close()
        return 3
    http.start()

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
        http.stop()
        bus.publish("daemon.stop", {"pid": os.getpid()})
        lock.clear_holder(paths.lock_path())
        data_db.close()
        events_db.close()
    return 0
