"""The atomic account-safety pacing engine — pure decision plus a config resolver.

Every outbound marketplace action reserves through this before it happens: it is the one
deterministic authority for "may I act on this marketplace right now?". The cap is keyed per
marketplace account (sell and buy share one ledger); `kind` is recorded for observability only,
never a separate cap bucket. Recording happens at reserve, so a crash between reserve and send
under-sends — the safe direction.

Verdicts: `go` (act after the returned jitter), `wait` (at the hourly cap; delay is when a slot
frees), `quiet` (inside quiet hours; delay is until the window ends). quiet hours and the cap are
checked BEFORE any jitter is chosen, so a mode can only change the jitter, never a safety floor.
FAST mode zeroes jitter, lifts the cap to its ceiling, and disables quiet hours — it drops the
account-safety disguise for a live demo and never auto-reverts.

The store wraps `evaluate` in one transaction (count-in-window → decide → record-on-go → compact);
the go-jitter is slept by the caller AFTER that transaction, never holding the DB lock.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

WINDOW_SECONDS = 3600  # the cap is per hour


@dataclass(frozen=True)
class PacingConfig:
    cap: int
    delay_min: float
    delay_max: float
    idelay_min: float
    idelay_max: float
    quiet_start: int
    quiet_end: int
    mode: str


def resolve(config, quiet_hours) -> PacingConfig:
    """Build the effective pacing config from the daemon config plus the quiet-hours window. The
    window is passed in explicitly — it is a runtime *setting* (read from the settings store by the
    tool layer), not a config knob, and engines never touch the store. The jitter/cap knobs are
    already validated and clamped to the hard ceilings at load; FAST mode is applied here — it
    zeroes both jitter ranges, lifts the cap to the ceiling, and disables quiet hours."""
    from selly_agent import config as config_mod

    reply = tuple(config.reply_delay_sec)
    interactive = tuple(config.interactive_reply_delay_sec)
    quiet = tuple(quiet_hours)
    if config.pacing_mode == "fast":
        return PacingConfig(
            cap=config_mod.HARD_CAP_CEILING,
            delay_min=0.0,
            delay_max=0.0,
            idelay_min=0.0,
            idelay_max=0.0,
            quiet_start=0,
            quiet_end=0,
            mode="fast",
        )
    return PacingConfig(
        cap=config.max_actions_per_hour,
        delay_min=reply[0],
        delay_max=reply[1],
        idelay_min=interactive[0],
        idelay_max=interactive[1],
        quiet_start=quiet[0],
        quiet_end=quiet[1],
        mode="normal",
    )


def in_quiet_hours(hour: int, start: int, end: int) -> bool:
    """Is `hour` inside [start, end)? Handles a window that wraps past midnight (e.g. 23..8)."""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _seconds_until_hour(now: float, target_hour: int) -> float:
    dt = datetime.fromtimestamp(now)
    delta_hours = (target_hour - dt.hour) % 24
    secs = delta_hours * 3600 - dt.minute * 60 - dt.second
    if secs <= 0:
        secs += 24 * 3600
    return float(secs)


def _seconds_until_slot_frees(timestamps: list, now: float) -> float:
    in_window = [t for t in timestamps if t > now - WINDOW_SECONDS]
    if not in_window:
        return 0.0
    return max(0.0, (min(in_window) + WINDOW_SECONDS) - now)


def evaluate(
    timestamps: list,
    *,
    now: float,
    cfg: PacingConfig,
    kind: str,
    interactive: bool = False,
) -> dict:
    """Pure decision over the marketplace's in-window action timestamps. Returns a verdict dict
    with `record` True only on `go` — quiet hours and the cap are checked before recording, so a
    blocked request never consumes a slot. `interactive` selects the jitter range only."""
    hour = datetime.fromtimestamp(now).hour
    in_window = [t for t in timestamps if t > now - WINDOW_SECONDS]
    count = len(in_window)
    base = {"kind": kind, "count": count, "cap": cfg.cap, "record": False}

    if in_quiet_hours(hour, cfg.quiet_start, cfg.quiet_end):
        return {**base, "verdict": "quiet", "delay_sec": _seconds_until_hour(now, cfg.quiet_end)}
    if count >= cfg.cap:
        return {**base, "verdict": "wait", "delay_sec": _seconds_until_slot_frees(in_window, now)}

    # A publish is a slow, human-paced browser action; a pre-click jitter adds no disguise, only
    # delay, so publishes are jitter-free. The cap and quiet hours above still apply.
    if kind == "publish":
        delay = 0.0
    else:
        lo, hi = (cfg.idelay_min, cfg.idelay_max) if interactive else (cfg.delay_min, cfg.delay_max)
        delay = random.uniform(lo, hi) if hi > 0 else 0.0
    return {**base, "verdict": "go", "delay_sec": round(delay, 2), "record": True}
