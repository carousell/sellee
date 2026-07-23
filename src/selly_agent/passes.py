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

from . import paths
from .harness import claude
from .harness.model import PassSpec
from .pass_stream import is_cap_hit, parse_stream_line
from .proc_tree import PASS_PROMPT_MARKER, confirm_dead, reap_strays
from .tools import TIER_PASS_PUBLISH, tools_for_tier

log = logging.getLogger(__name__)

PASS_MAX_TURNS = 30
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


def publish_prompt(item_id: str) -> str:
    return (
        f"{PASS_PROMPT_MARKER}\n"
        f"You are a headless selly-agent pass. Publish item {item_id} to carousell.ai using ONLY "
        f"your MCP tools: read the item with get_item, then call carousell_ai_publish_listing. "
        f"Do not attempt any other tool. When the listing is published, stop."
    )


def allowed_tools_for_publish(server_name: str = "selly") -> tuple:
    return tuple(f"mcp__{server_name}__{spec.name}" for spec in tools_for_tier(TIER_PASS_PUBLISH))


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


def build_spec(item_id: str, endpoint: str, token: str, model: str) -> PassSpec:
    return PassSpec(
        prompt=publish_prompt(item_id),
        model=model,
        mcp_endpoint=endpoint,
        mcp_token=token,
        allowed_tools=allowed_tools_for_publish(),
        max_turns=PASS_MAX_TURNS,
    )


def _write_workspace(workspace: Path, spec: PassSpec) -> None:
    for rel, content in claude.render_workspace(spec).items():
        path = workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def _cleanup_workspace(workspace: Path) -> None:
    shutil.rmtree(workspace, ignore_errors=True)


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
    item_id = (claimed.payload or {}).get("item_id")
    if not item_id:
        deps.store.finish_pass(
            pass_id, status="error", rc=None, cls="spawn_error", summary="no item_id in payload"
        )
        deps.bus.publish(
            "pass.end",
            {"class": "spawn_error", "is_error": True, "error": "no item_id in payload"},
            pass_id=pass_id,
        )
        return "spawn_error"

    deadline_sec = float(deps.config.pass_deadline_sec)
    expiry = deps.now() + deadline_sec + TOKEN_SLACK_SEC
    token = deps.auth.mint_pass_token(TIER_PASS_PUBLISH, pass_id, expiry)
    workspace = paths.pass_workspace_dir(pass_id)
    deps.bus.publish("pass.start", {"type": claimed.type, "item_id": item_id}, pass_id=pass_id)

    proc = None
    pgid = None
    try:
        spec = build_spec(item_id, deps.http_endpoint, token, deps.config.pass_model)
        _write_workspace(workspace, spec)
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

        stderr_path = paths.pass_stderr_log(pass_id)
        with open(stderr_path, "wb") as errf:
            proc = subprocess.Popen(  # noqa: S603 — argv is composed by our emitter, not a shell
                argv,
                cwd=str(workspace),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=errf,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
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
                "item_id": item_id,
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
