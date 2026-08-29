"""The atomic account-safety pacing engine — pure decision plus a config resolver.

Every outbound marketplace action reserves through this before it happens: it is the one
deterministic authority for "may I act on this marketplace right now?". The cap is keyed per
marketplace account (sell and buy share one ledger); `kind` is recorded for observability only,
never a separate cap bucket. Recording happens at reserve, so a crash between reserve and send
under-sends — the safe direction.

Verdicts: `go` (act after the returned jitter), `wait` (at the hourly cap; delay is when a slot
frees), `quiet` (inside quiet hours; delay is until the window ends). quiet hours and the cap are
checked BEFORE any jitter is chosen, so a mode can only change the jitter, never a safety floor.
Quiet hours hold only the kinds we *start* — see REACTIVE_KINDS; the cap holds every kind.
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

# The kinds quiet hours do NOT hold. The window is an account-safety disguise, and what gives an
# automated account away is *starting* things at 4am — a cold nudge, a follow-up, a burst of
# listings. Answering a buyer who just wrote is the opposite: they are awake, they are waiting, and
# a seller who replies is the most ordinary thing on the marketplace.
#
# Holding these was how a buyer's 03:14 "still available?" sat unanswered until 08:00 while the
# reply lane respawned a pass every 28 seconds to be refused again. The cap below still applies to
# every kind, so exempting these loosens exactly one gate and never account safety itself.
REACTIVE_KINDS = ("reply", "holding")


@dataclass(frozen=True)
class PacingConfig:
    cap: int
    delay_min: float
    delay_max: float
    idelay_min: float
    idelay_max: float
    quiet_start_min: int  # minutes since midnight
    quiet_end_min: int
    mode: str


def resolve(config, quiet_hours) -> PacingConfig:
    """Build the effective pacing config from the daemon config plus the quiet-hours window. The
    window — a [start, end] pair of minutes since midnight — is passed in explicitly: it is a
    runtime *setting* (read from the settings store by the tool layer), not a config knob, and
    engines never touch the store. The jitter/cap knobs are already validated and clamped to the
    hard ceilings at load; FAST mode is applied here — it zeroes both jitter ranges, lifts the cap
    to the ceiling, and disables quiet hours."""
    from sellee import config as config_mod

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
            quiet_start_min=0,
            quiet_end_min=0,
            mode="fast",
        )
    return PacingConfig(
        cap=config.max_actions_per_hour,
        delay_min=reply[0],
        delay_max=reply[1],
        idelay_min=interactive[0],
        idelay_max=interactive[1],
        quiet_start_min=quiet[0],
        quiet_end_min=quiet[1],
        mode="normal",
    )


def in_quiet_window(minute_of_day: int, start: int, end: int) -> bool:
    """Is `minute_of_day` (0..1439, minutes since midnight) inside [start, end)? Handles a window
    that wraps past midnight (e.g. 23:00..08:00). start == end disables the window."""
    if start == end:
        return False
    if start < end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end


def _seconds_until_minute_of_day(now: float, target_min: int) -> float:
    """Seconds from `now` until the wall clock next reaches `target_min` minutes past midnight."""
    dt = datetime.fromtimestamp(now)
    delta_min = (target_min - (dt.hour * 60 + dt.minute)) % (24 * 60)
    secs = delta_min * 60 - dt.second
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
    dt = datetime.fromtimestamp(now)
    minute_of_day = dt.hour * 60 + dt.minute
    in_window = [t for t in timestamps if t > now - WINDOW_SECONDS]
    count = len(in_window)
    base = {"kind": kind, "count": count, "cap": cfg.cap, "record": False}

    if kind not in REACTIVE_KINDS and in_quiet_window(
        minute_of_day, cfg.quiet_start_min, cfg.quiet_end_min
    ):
        return {
            **base,
            "verdict": "quiet",
            "delay_sec": _seconds_until_minute_of_day(now, cfg.quiet_end_min),
        }
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
