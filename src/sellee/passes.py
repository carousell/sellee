"""The pass runner: claim a queued pass, spawn a headless harness pass, babysit it, ledger it.

Single-flight by construction — the scheduler's in-flight guard plus a claim that stamps `running`
in one transaction mean two claimers never take the same row, and a crash mid-pass is failed
loudly by the stale-running sweep (never silently re-run). Each pass runs in an empty per-pass
workspace holding only its generated harness config; its stdout (stream-json) is parsed live into
bus events, a babysitter enforces the deadline and daemon-stop via a process-group kill, and every
attempt is ledgered as pass.start / pass.end so a lane that fails every attempt is visible.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sellee import marketplaces, paths, settings, skills
from sellee import reply_prompt as reply_prompt_mod
from sellee.browser import chrome
from sellee.browser import client as browser_client
from sellee.browser import markets as market_adapters
from sellee.channel import prompt as channel_prompt_mod
from sellee.harness import claude
from sellee.harness.model import PassSpec, StdioServer
from sellee.pass_stream import is_cap_hit, parse_stream_line
from sellee.proc_tree import PASS_PROMPT_MARKER, confirm_dead, reap_strays
from sellee.store import Scope
from sellee.tools import (
    TIER_PASS_CHANNEL,
    TIER_PASS_PUBLISH,
    TIER_PASS_REPLY,
    tools_for_tier,
)

log = logging.getLogger(__name__)

# Runaway backstops, not budgets, so they are sized per flow rather than shared across flows.
PASS_MAX_TURNS = 30
# Filling a marketplace composer takes many more calls than answering a buyer.
PUBLISH_MAX_TURNS = 80
# The ephemeral token (and the reaper's age gate) outlive the deadline by this slack, so a pass
# in its final teardown is never de-authed or reaped out from under itself.
TOKEN_SLACK_SEC = 60.0
_BABY_POLL_SEC = 0.1
# How often the babysitter checks the pause flag — a pause interrupts a running pass within about
# this window (INV-29's mid-flight interrupt; the read is a cheap single-row SQLite query).
_PAUSE_WATCH_SEC = 1.0
# confirm_dead can spend up to grace + kill-wait seconds; give the babysitter join headroom.
_GRACE_JOIN_SEC = 20.0


class SpawnError(Exception):
    """The pass could not be launched (binary missing, bad payload). A loud, ledgered outcome."""


class PassPayloadError(Exception):
    """A pass's payload is missing/invalid — a loud, ledgered spawn_error, never a silent skip."""


DEFAULT_PUBLISH_MARKET = marketplaces.RAIL

# The browser tools a publish pass may call — the diet, not the whole Playwright surface. Every tool
# schema rides every turn of the pass, so a name that no recipe step uses is paid for on every run.
#
# Three exclusions are deliberate rather than incidental:
#   * no browser_close — it would close the seller's warm Chrome out from under everything else;
#   * no browser_run_code_unsafe — arbitrary Playwright code is the browser's equivalent of a shell,
#     and this whole surface exists so that a pass has typed verbs instead of one;
#   * no browser_take_screenshot — every check in the recipe is a DOM read-back, which is both
#     cheaper and harder to misread than a picture.
PUBLISH_BROWSER_TOOLS = (
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_press_key",
    "browser_file_upload",
    "browser_wait_for",
    "browser_tabs",
    "browser_handle_dialog",
    "browser_evaluate",
)
BROWSER_SERVER_NAME = "playwright"


def publish_prompt(
    item_id: str,
    market: str = DEFAULT_PUBLISH_MARKET,
    *,
    photos: tuple = (),
    composer_url: str | None = None,
) -> str:
    where = (
        "to carousell.ai, following the listing flow's publish step"
        if market == DEFAULT_PUBLISH_MARKET
        else f"to {marketplaces.display_name(market)} in the browser, following its listing recipe"
    )
    # The browser reads only its own workspace roots, so the photos are staged there and the prompt
    # names them; the recipe never types a marketplace URL, so the composer arrives here too.
    staged = f"Its photos are in your working directory: {', '.join(photos)}\n" if photos else ""
    composer = f"The composer is at {composer_url}\n" if composer_url else ""
    return (
        f"{PASS_PROMPT_MARKER}\n"
        f"Publish item {item_id} {where}.\n"
        f"Read the item with get_item. It has already been confirmed with the seller, so do not "
        f"re-confirm and do not change its title, price, or description — publish what is there.\n"
        f"{composer}"
        f"{staged}"
        f"Report the live listing URL when it is up, or say what failed."
    )


def publish_market(payload: dict) -> str:
    """Which marketplace a publish pass is for. Absent means the rail, which is what every publish
    enqueued before browser markets existed meant."""
    return str(payload.get("market") or DEFAULT_PUBLISH_MARKET)


def staged_photo_names(item_id: str, market: str, store) -> tuple:
    """The filenames an item's photos are staged under in a browser publish's workspace.

    Derived rather than returned by the copy, because the prompt is built before the workspace
    exists; one helper answers for both so the names cannot drift from the files. Empty for the
    rail, whose upload runs in the daemon and reads the media store directly.
    """
    if market == DEFAULT_PUBLISH_MARKET:
        return ()
    item = store.get_item(item_id)
    if item is None:
        return ()
    return tuple(
        f"{index:02d}{Path(entry['path']).suffix.lower()}"
        for index, entry in enumerate(item.get("photos") or [], start=1)
        if entry.get("path")
    )


def _publish_market_error(market: str, region: str | None) -> str | None:
    """Why this market cannot be published to, or None when it can."""
    if market == DEFAULT_PUBLISH_MARKET:
        return None
    if marketplaces.get_marketplace(market) is None:
        return f"no marketplace {market!r} in the registry"
    publishable = market_adapters.publishable_markets(region)
    if market not in publishable:
        supported = ", ".join(publishable) or "none"
        return f"cannot publish to {market!r} (publishable here: {supported})"
    return None


def validate_payload(pass_type: str, payload: dict, store) -> None:
    """Raise PassPayloadError if this payload could only ever produce a failed pass.

    Both the runner and the control route ask this — the runner so a pass enqueued in process
    fails loudly instead of spawning, the route so a person at a terminal hears their own typo
    instead of being handed the id of a pass that was already doomed. A mistyped market is the
    case that motivated it: `display_name` falls back to the id and the connector lookup matches
    neither kind, so the pass was told to publish somewhere in a browser it never got.
    """
    if pass_type == "publish":
        if not payload.get("item_id"):
            raise PassPayloadError("no item_id in payload")
        reason = _publish_market_error(publish_market(payload), store.seller_region())
        if reason:
            raise PassPayloadError(reason)


def _publish_prompt(payload: dict, store, pass_id: str) -> str:
    item_id = payload["item_id"]
    market = publish_market(payload)
    return publish_prompt(
        item_id,
        market,
        photos=staged_photo_names(item_id, market, store),
        composer_url=marketplaces.market_url(market, "sell", store.seller_region()),
    )


def _publish_skills(payload: dict, store, pass_id: str) -> tuple:
    """The conventions plus this market's own publish recipe.

    Which recipe comes from the registry rather than a branch here, so adding a marketplace is a
    registry entry and a skill file. A market with no recorded recipe gets the conventions alone
    rather than another market's steps.
    """
    recipe = marketplaces.listing_flow(publish_market(payload))
    return ("sellee-conventions",) + ((recipe,) if recipe else ())


def _publish_browser_tools(payload: dict, store, pass_id: str) -> tuple:
    """The browser diet, and only for a market the agent drives in Chrome.

    A rail publish talks to an API and is handed no browser at all — browser authority follows the
    market, not the pass type.
    """
    if marketplaces.connector_type(publish_market(payload)) != "browser":
        return ()
    return PUBLISH_BROWSER_TOOLS


def _reply_prompt(payload: dict, store, pass_id: str) -> str:
    thread_ids = payload.get("thread_ids") or []
    if not thread_ids:
        raise PassPayloadError("no thread_ids in payload")
    # Read through the store the pass itself will use, so the prompt can only carry what the pass is
    # scoped to see; a thread that has been closed since the lane claimed it simply drops out.
    threads = [t for t in (store.get_thread(tid) for tid in thread_ids) if t is not None]
    items = {}
    for thread in threads:
        item_id = thread.get("item_id")
        if item_id and item_id not in items:
            item = store.get_item(item_id)
            if item is not None:
                items[item_id] = item
    return reply_prompt_mod.build_reply_prompt(threads, items)


def _channel_prompt(payload: dict, store, pass_id: str) -> str:
    # The rows claimed into this pass at enqueue, plus the conversational window — both read fresh
    # from durable sellee.db rows, so a restart rebuilds the same prompt.
    claimed = store.inbox_for_pass(pass_id)
    transcript = store.recent_transcript(channel_prompt_mod.TRANSCRIPT_WINDOW_LIMIT)
    return channel_prompt_mod.build_channel_prompt(
        claimed, transcript, settings_block=settings.prompt_block(store)
    )


def _channel_media_paths(payload: dict, store, pass_id: str) -> tuple:
    """Exactly the media paths this pass's prompt shows it — the rows claimed into it, plus any
    still in the conversational window.

    The window half is what lets a listing flow span passes: the photo arrives in one pass, the
    price is agreed in the next, and the publish happens in a third. Scoping the grant to the
    claimed rows alone would leave the later passes able to see a path they cannot open.
    """
    claimed = store.inbox_for_pass(pass_id)
    transcript = store.recent_transcript(channel_prompt_mod.TRANSCRIPT_WINDOW_LIMIT)
    paths_seen = {path: None for row in claimed for path in (row.get("media_paths") or [])}
    for path in channel_prompt_mod.transcript_media_paths(transcript):
        paths_seen[path] = None
    return tuple(paths_seen)


def _no_media_paths(payload: dict, store, pass_id: str) -> tuple:
    return ()


def _no_browser_tools(payload: dict, store, pass_id: str) -> tuple:
    return ()


def _full_scope(payload: dict) -> object | None:
    """No entity scope: the pass may reach any row its tier allows.

    Right for the flows whose counterpart is the trusted seller, and for a publish, which touches
    only the item it was given. A flow processing a buyer's words gets a real scope instead.
    """
    return None


def _reply_scope(payload: dict):
    """Exactly the threads this pass was spawned for, plus the items they are about.

    The reply pass is the one flow that acts on text a stranger wrote, so it is held to the entities
    the lane claimed for it: a thread outside the pass reads as absent rather than forbidden.

    The bound is the **pass**, not the thread. `enqueue_reply_pass` deliberately coalesces every
    waiting thread into one pass and this mints one scope over all of them, so within a pass a
    buyer's message sits in the same prompt as, and in scope with, every other buyer coalesced
    into it. What the scope stops is reaching a thread the lane did not claim — it is not
    per-buyer isolation.
    """
    return Scope.of(
        threads=payload.get("thread_ids") or (),
        items=payload.get("item_ids") or (),
    )


@dataclass(frozen=True)
class PassType:
    """A pass type's spec: the tier its token carries (which tools it may call), the skills that
    make up its system prompt, whether it may research on the web, how its task prompt is built
    from the queued payload, and which media files it may read (a model views an image by reading
    the file, so a photo-handling type grants its claimed media — file access stays denied
    otherwise). New types register here rather than forking run_pass.

    Skills are per-type and minimal. A pass carrying a rulebook it never acts on pays for it on
    every run, and a rule stated to a flow that cannot follow it is noise.
    """

    tier: str
    build_prompt: Callable[[dict, object, str], str]
    skills: tuple = ()
    web_tools: bool = False
    build_media_paths: Callable[[dict, object, str], tuple] = _no_media_paths
    # Which browser tools this pass may call, decided from its payload.
    build_browser_tools: Callable[[dict, object, str], tuple] = _no_browser_tools
    # The entity scope the pass's token carries. Enforced per request at every row load, so a scoped
    # pass cannot read or write outside what it was spawned for.
    build_scope: Callable[[dict], object] = _full_scope
    # Set when the skill set depends on the payload (which marketplace's recipe a publish needs);
    # otherwise `skills` is the declaration and stays readable at a glance.
    build_skills: Callable[[dict, object, str], tuple] | None = None
    max_turns: int = PASS_MAX_TURNS

    def skills_for(self, payload: dict, store=None, pass_id: str = "") -> tuple:
        if self.build_skills is None:
            return self.skills
        return self.build_skills(payload, store, pass_id)


PASS_TYPES = {
    # Publish is a narrow, already-decided job: the seller signed off on the numbers in the
    # conversation that produced the draft, so this pass needs the flow's publish discipline and
    # nothing about voice — it talks to no one, and the photo upload is daemon-side, so it never
    # needs to see an image either.
    # One publish type serves both kinds of marketplace, the rail and a browser market: what differs
    # is derived from the payload (see `_publish_skills` / `_publish_browser_tools`).
    "publish": PassType(
        tier=TIER_PASS_PUBLISH,
        build_prompt=_publish_prompt,
        build_skills=_publish_skills,
        build_browser_tools=_publish_browser_tools,
        max_turns=PUBLISH_MAX_TURNS,
    ),
    # The reply pass answers buyers. It is the one flow acting on words a stranger wrote, so it is
    # the most constrained: an entity scope covering only its own threads, no web research, and no
    # browser — the send goes through the daemon's sink, which the LLM never touches. What it needs
    # is voice, the buyer rulebook, and the scam guard.
    "reply": PassType(
        tier=TIER_PASS_REPLY,
        build_prompt=_reply_prompt,
        skills=("sellee-conventions", "voice-and-style", "buyer-conversation", "scam-guard"),
        build_scope=_reply_scope,
    ),
    # The channel pass is the seller conversation: it writes to a human, so it needs voice and the
    # escalation copy, and it runs the listing flow end to end — including the comps research the
    # flow's pricing step calls for, and eyes on the photos the seller sent.
    "channel": PassType(
        tier=TIER_PASS_CHANNEL,
        build_prompt=_channel_prompt,
        skills=("sellee-conventions", "voice-and-style", "seller-comms", "listing-flow"),
        web_tools=True,
        build_media_paths=_channel_media_paths,
    ),
}


def allowed_tools_for(tier: str, server_name: str = "sellee") -> tuple:
    return tuple(f"mcp__{server_name}__{spec.name}" for spec in tools_for_tier(tier))


def browser_command(config) -> tuple:
    """The Playwright MCP invocation a browser-driving pass spawns for itself.

    Each pass gets its own instance, spawned by the harness inside the pass's process group, so it
    dies with the pass and the daemon's own instance is untouched. Both attach to the same warm
    Chrome over CDP.

    The port is resolved here, at spawn time, rather than carried from anywhere earlier: unpinned it
    is whatever the live Chrome announced, and a pass built against a port from a Chrome that has
    since restarted would dial nothing.
    """
    configured = getattr(config, "playwright_mcp_cmd", None)
    if configured:
        return tuple(configured)
    port = chrome.resolve_port(getattr(config, "chrome_cdp_port", None))
    return tuple(browser_client.default_command(browser_client.cdp_endpoint(port)))


def resolve_claude_bin(config) -> str | None:
    """The `claude` binary: an explicit config path if set and present, else PATH, else the
    conventional user install locations. None (not a crash) when nothing resolves."""
    if config.claude_bin:
        return config.claude_bin if Path(config.claude_bin).exists() else None
    found = shutil.which("claude")
    if found:
        return found
    for candidate in paths.claude_bin_candidates():
        if candidate.exists():
            return str(candidate)
    return None


def default_argv_builder(config) -> Callable[[PassSpec], list]:
    def build(spec: PassSpec) -> list:
        binary = resolve_claude_bin(config)
        if binary is None:
            raise SpawnError("claude binary not found — set claude_bin in config or add it to PATH")
        return claude.pass_argv(spec, binary)

    return build


@dataclass
class PassDeps:
    bus: object
    store: object
    config: object
    auth: object  # HttpServer.auth — mint_pass_token / revoke_pass_token
    http_endpoint: str
    stop_event: threading.Event
    argv_builder: Callable[[PassSpec], list]
    tracked_pgids: set = field(default_factory=set)
    now: Callable[[], float] = time.time


def _contained_media_paths(raw_paths) -> tuple:
    """Resolve and containment-check media paths before they become a Read grant.

    The store already refuses to persist a path outside the media store, so anything failing here
    is a bug, not input — it raises rather than silently narrowing the grant (a pass that can't
    see a photo the prompt promises it would just burn turns discovering that).
    """
    root = paths.media_dir().resolve()
    out = []
    for raw in raw_paths:
        resolved = Path(raw).resolve()
        if root != resolved and root not in resolved.parents:
            raise ValueError(f"media path escapes the media store: {raw!r}")
        out.append(str(resolved))
    return tuple(out)


def build_spec(
    prompt: str,
    endpoint: str,
    token: str,
    model: str,
    pass_type: PassType,
    media_paths: tuple = (),
    payload: dict | None = None,
    browser_tools: tuple = (),
    browser_command: tuple = (),
) -> PassSpec:
    browser_server = None
    if browser_tools:
        if not browser_command:
            raise SpawnError(
                "this pass needs the browser but no Playwright MCP command is configured — "
                "set playwright_mcp_cmd, or install Node so the npx default resolves"
            )
        browser_server = StdioServer(
            name=BROWSER_SERVER_NAME,
            command=browser_command[0],
            args=tuple(browser_command[1:]),
            tools=tuple(browser_tools),
        )
    return PassSpec(
        prompt=prompt,
        model=model,
        mcp_endpoint=endpoint,
        mcp_token=token,
        allowed_tools=allowed_tools_for(pass_type.tier),
        max_turns=pass_type.max_turns,
        append_system_prompt=skills.compose_system_prompt(pass_type.skills_for(payload or {}))
        or None,
        web_tools=pass_type.web_tools,
        readable_paths=_contained_media_paths(media_paths),
        browser_server=browser_server,
    )


def _write_workspace(workspace: Path, spec: PassSpec) -> None:
    # 0700 at creation: a browser publish stages the item's photographs in here, and the mode has to
    # be right before anything is written rather than chmodded afterwards.
    paths.ensure_private_dir(workspace)
    for rel, content in claude.render_workspace(spec).items():
        path = workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def _stage_photos(workspace: Path, payload: dict, store) -> tuple:
    """Copy a browser publish's photos into its workspace, and answer with what was staged.

    The browser's file upload reads only the directories the Playwright server treats as workspace
    roots, and the media store is not one — so a photo has to be put in front of it deliberately.
    The pass workspace keeps that grant as narrow as the flow, and is swept with the pass.

    A missing source file is skipped rather than fatal: the recipe verifies what it uploaded and
    reports a shortfall, which beats refusing to publish because one photo went astray.
    """
    item_id = payload.get("item_id")
    if not item_id:
        return ()
    names = staged_photo_names(item_id, publish_market(payload), store)
    if not names:
        return ()
    item = store.get_item(item_id)
    staged = []
    for name, entry in zip(names, item.get("photos") or []):
        source = Path(entry["path"])
        if not source.is_file():
            log.warning("photo %s for %s is missing; not staged", source, item_id)
            continue
        workspace.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, workspace / name)
        staged.append(name)
    return tuple(staged)


def _cleanup_workspace(workspace: Path) -> None:
    shutil.rmtree(workspace, ignore_errors=True)


def _write_prompt(proc, prompt: str) -> None:
    """Hand the pass its prompt on stdin and close, so the harness sees EOF and starts its turn.

    On its own thread, because the write can block: a coalesced reply prompt holds every waiting
    buyer's conversation and can exceed a pipe buffer, so a harness that is not reading yet leaves
    it parked mid-prompt. Inline, that would strand the spawn before the babysitter exists to time
    it out; from a thread, the kill at the deadline closes the read end and unblocks it.

    A harness that died before reading gives EPIPE, which is not worth raising — the exit code it
    already has classifies the pass.
    """
    if proc.stdin is None:
        return

    def _pump() -> None:
        try:
            proc.stdin.write(prompt)
        except OSError:
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    threading.Thread(target=_pump, daemon=True).start()


def _reader(proc, bus, pass_id: str, holder: dict) -> None:
    try:
        for raw in proc.stdout:
            for kind, payload in parse_stream_line(raw):
                bus.publish(kind, payload, pass_id=pass_id)
                if kind == "pass.result":
                    holder["result"] = payload
    except (OSError, ValueError):
        pass


def _babysitter(proc, deadline_sec: float, stop_event, killed: dict, paused_check=None) -> None:
    start = time.monotonic()
    last_pause_check = start
    while proc.poll() is None:
        now = time.monotonic()
        if now - start >= deadline_sec:
            killed["timeout"] = True
            confirm_dead(proc)
            return
        if stop_event is not None and stop_event.is_set():
            killed["stopped"] = True
            confirm_dead(proc)
            return
        # A pause interrupts a running pass within one watch window — safe because cursors/pacing
        # make the killed step re-runnable, so nothing is half-done.
        if paused_check is not None and now - last_pause_check >= _PAUSE_WATCH_SEC:
            last_pause_check = now
            if paused_check():
                killed["paused"] = True
                confirm_dead(proc)
                return
        time.sleep(_BABY_POLL_SEC)


def _classify(rc: int | None, killed: dict, result: dict | None) -> str:
    if killed.get("paused"):
        return "paused"
    if killed.get("timeout"):
        return "timeout"
    if killed.get("stopped"):
        return "stopped"
    if rc == 0:
        return "ok"
    if is_cap_hit(result):
        return "cap_hit"
    return "error"


def _summary(cls: str, result: dict | None) -> str:
    if result and result.get("num_turns") is not None:
        return f"{cls} (turns={result['num_turns']})"
    return cls


def run_pass(deps: PassDeps, claimed) -> str:
    """Run one claimed pass to completion and return its outcome class. Always ledgers pass.end,
    revokes the token, and sweeps the workspace — even on a forced kill."""
    pass_id = claimed.pass_id
    pass_type = PASS_TYPES.get(claimed.type)
    if pass_type is None:
        return _spawn_error(deps, claimed, pass_id, f"unknown pass type {claimed.type!r}")
    payload = claimed.payload or {}
    try:
        validate_payload(claimed.type, payload, deps.store)
        prompt = pass_type.build_prompt(payload, deps.store, pass_id)
        media_paths = pass_type.build_media_paths(payload, deps.store, pass_id)
        browser_tools = pass_type.build_browser_tools(payload, deps.store, pass_id)
    except PassPayloadError as exc:
        return _spawn_error(deps, claimed, pass_id, str(exc))

    deadline_sec = float(deps.config.pass_deadline_sec)
    expiry = deps.now() + deadline_sec + TOKEN_SLACK_SEC
    token = deps.auth.mint_pass_token(
        pass_type.tier, pass_id, expiry, scope=pass_type.build_scope(payload)
    )
    workspace = paths.pass_workspace_dir(pass_id)
    deps.bus.publish("pass.start", {"type": claimed.type}, pass_id=pass_id)

    proc = None
    pgid = None
    try:
        try:
            spec = build_spec(
                prompt,
                deps.http_endpoint,
                token,
                deps.config.pass_model,
                pass_type,
                media_paths=media_paths,
                payload=payload,
                browser_tools=browser_tools,
                browser_command=browser_command(deps.config),
            )
        except SpawnError as exc:
            deps.store.finish_pass(
                pass_id, status="error", rc=None, cls="spawn_error", summary=str(exc)
            )
            deps.bus.publish(
                "pass.end",
                {"type": claimed.type, "class": "spawn_error", "is_error": True, "error": str(exc)},
                pass_id=pass_id,
            )
            return "spawn_error"
        _write_workspace(workspace, spec)
        if claimed.type == "publish":
            _stage_photos(workspace, payload, deps.store)
        try:
            argv = deps.argv_builder(spec)
        except SpawnError as exc:
            deps.store.finish_pass(
                pass_id, status="error", rc=None, cls="spawn_error", summary=str(exc)
            )
            deps.bus.publish(
                "pass.end",
                {"type": claimed.type, "class": "spawn_error", "is_error": True, "error": str(exc)},
                pass_id=pass_id,
            )
            return "spawn_error"

        # Owner-only: harness stderr can quote prompt content, i.e. buyer conversations.
        stderr_path = paths.ensure_private_file(paths.pass_stderr_log(pass_id))
        with open(stderr_path, "wb") as errf:
            proc = subprocess.Popen(  # noqa: S603 — argv is composed by our emitter, not a shell
                argv,
                cwd=str(workspace),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errf,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            # The prompt goes here rather than in argv: it is the seller's item data and, for a
            # reply, the verbatim buyer conversation, and argv is world-readable via `ps`.
            _write_prompt(proc, spec.prompt)

            try:
                pgid = os.getpgid(proc.pid)
                deps.tracked_pgids.add(pgid)
            except OSError:
                pgid = None

            holder: dict = {}
            killed: dict = {}
            reader = threading.Thread(
                target=_reader, args=(proc, deps.bus, pass_id, holder), daemon=True
            )
            baby = threading.Thread(
                target=_babysitter,
                args=(proc, deadline_sec, deps.stop_event, killed, deps.store.is_paused),
                daemon=True,
            )
            reader.start()
            baby.start()
            rc = proc.wait()
            reader.join(timeout=5)
            baby.join(timeout=_GRACE_JOIN_SEC)

        result = holder.get("result")
        cls = _classify(rc, killed, result)
        status = "done" if cls == "ok" else "error"
        deps.store.finish_pass(
            pass_id, status=status, rc=rc, cls=cls, summary=_summary(cls, result)
        )
        deps.bus.publish(
            "pass.end",
            {
                "type": claimed.type,
                "rc": rc,
                "class": cls,
                "is_error": cls != "ok",
                "session_id": (result or {}).get("session_id"),
                "usage": (result or {}).get("usage"),
            },
            pass_id=pass_id,
        )
        return cls
    finally:
        deps.auth.revoke_pass_token(token)
        if pgid is not None:
            deps.tracked_pgids.discard(pgid)
        _cleanup_workspace(workspace)


def _spawn_error(deps: PassDeps, claimed, pass_id: str, reason: str) -> str:
    """Ledger a pass that could never start (unknown type, bad payload) loudly and return."""
    deps.store.finish_pass(pass_id, status="error", rc=None, cls="spawn_error", summary=reason)
    deps.bus.publish(
        "pass.end",
        {"type": claimed.type, "class": "spawn_error", "is_error": True, "error": reason},
        pass_id=pass_id,
    )
    return "spawn_error"


def pass_lane(deps: PassDeps) -> None:
    """One scheduler tick of the pass lane: fail crashed passes loudly, then run one queued pass.
    The scheduler's in-flight guard keeps this from overlapping itself (single-flight)."""
    stale = deps.store.fail_stale_running(deadline_slack(deps.config), now=deps.now())
    for pid in stale:
        deps.bus.publish("pass.end", {"class": "stale", "is_error": True}, pass_id=pid)
    # A paused daemon runs but acts on nothing: the lane claims no queued pass while paused (the
    # babysitter has already killed any in-flight one). /resume lets the next tick claim again.
    if deps.store.is_paused():
        return
    claimed = deps.store.claim_queued_pass()
    if claimed is None:
        return
    run_pass(deps, claimed)


def stray_reaper(deps: PassDeps) -> None:
    for stray in reap_strays(deps.tracked_pgids, deadline_slack(deps.config)):
        deps.bus.publish(
            "pass.reaped",
            {"pid": stray["pid"], "pgid": stray["pgid"], "age_sec": stray["age_sec"]},
        )


def deadline_slack(config) -> float:
    return float(config.pass_deadline_sec) + TOKEN_SLACK_SEC
