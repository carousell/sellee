"""Daemon configuration — read-only from the daemon's side.

Reads config.json from the config dir. A missing file means all defaults: the daemon never
writes config (config writers are the installer and tools). Invalid values are rejected at
startup with a clear error rather than sanitized — a fat-fingered config should fail loud,
not silently become something the user didn't ask for. Unknown keys warn and are ignored so
a newer config stays readable by an older build across an update.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit

from sellee import paths

log = logging.getLogger(__name__)

_VALID_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
_VALID_DAEMON_MODES = {"login-start", "manual"}
_VALID_PACING_MODES = {"normal", "fast"}

# Hosts a URL-typed config value may reach over plain http. Cleartext to a real network host is
# refused: the Telegram and Discord bases put the bot token in the URL path, and the release base
# serves code this machine then executes. Loopback is exempt — there is no network to intercept,
# and it is what the fake API servers and the staged-release tests listen on.
_PLAINTEXT_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Account-safety ceilings, in code. Pacing config is validated in two directions: a malformed
# value is rejected (fail loud, like every other knob), but a well-formed value that is *looser*
# than these ceilings is clamped down — a tampered or fat-fingered config can only ever tighten
# pacing, never relax it past the ceiling. The delay ceiling also bounds how long a healthy
# send intent can sit between reserve and send, so the stale-intent sweep's grace window can
# never fold a merely-jittered send as a crash orphan.
HARD_CAP_CEILING = 60
HARD_DELAY_CEILING_SEC = 3.0


class ConfigError(Exception):
    """A config value is present but invalid. Raised at startup; never sanitized away."""


@dataclass(frozen=True)
class Config:
    log_level: str = "INFO"
    tick_interval_sec: float = 5.0
    retention_days: int = 14
    # Routine-tier events (the per-attempt task.start/task.ok ledger) age out on a much shorter
    # window than retention_days — they're ~99% of event volume and no consumer reads them by
    # name, so keeping them the full window is mostly dead weight (and it multiplies across DB
    # backups).
    routine_events_retention_hours: int = 24
    backups_keep: int = 5
    # Recorded by the installer, read by daemon status. Not consumed by the daemon loop.
    daemon_mode: str = "manual"
    daemon_label: str | None = None
    # A fixed port keeps generated harness configs stable across daemon restarts.
    http_port: int = 7355
    pass_deadline_sec: float = 900.0
    pass_model: str = "sonnet"
    # Explicit path to the harness CLI; null means resolve from PATH (plus the
    # conventional user install locations) at spawn time.
    claude_bin: str | None = None
    # One or more directories, colon-joined — a PATH fragment reaching `node` and `npx`, recorded by
    # the installer. A supervised daemon is given a minimal PATH that contains no version manager's
    # shims, so without this the browser server cannot be spawned at all; it is prepended to the
    # PATH of the job the supervisor installs. Null means the daemon relies on whatever PATH it
    # inherits, which is right when it is started from a shell.
    node_bin_dir: str | None = None
    carousell_ai_api_base: str = "https://api.carousell.ai"
    carousell_ai_web_base_url: str = "https://www.carousell.ai"
    # The Telegram Bot API base. Overridable so the channel tests point the real transport at a
    # local fake server; production uses the default.
    telegram_api_base: str = "https://api.telegram.org"
    # The Discord REST API base. Overridable so the channel tests point the real transport at a
    # local fake server; production uses the default.
    discord_api_base: str = "https://discord.com/api/v10"
    # The warm Chrome's CDP port. Null — the default — lets Chrome choose a free one and announce
    # it inside the profile directory: nothing can bind a port before it is chosen, and a port only
    # readable out of a 0700 directory is not one another local user can find. Pin an integer where
    # the port is an agreement with a process that cannot read that file: a container's forwarder,
    # or a Chrome someone starts by hand.
    chrome_cdp_port: int | None = None
    # The Chrome executable to start when none is running. Null resolves to the OS default install
    # path, which is what a normal install has.
    chrome_bin: str | None = None
    # The Playwright MCP server the daemon spawns as a stdio subprocess, as an argv list. Null
    # resolves to the npx default at spawn time. Pinning it avoids an npx cold resolution — a
    # network fetch — landing on the hot path.
    playwright_mcp_cmd: list | None = None
    # How often the inbox lane reads the marketplace, and how many of those ticks use the skip gate
    # before one opens every active thread regardless. The sweep is the backstop for a lying inbox
    # preview: a missed message costs one sweep interval of latency, never a stranded buyer.
    inbox_read_interval_sec: float = 300.0
    inbox_full_sweep_every: int = 6
    # Consecutive failed marketplace reads before one needs-me escalation. A market that cannot be
    # seen must never look like a market with no news.
    browser_blind_after: int = 3
    # How long the send read-back keeps looking for its own bubble before giving up and calling the
    # send unverified. A chat that commits the message to its server and re-renders afterwards is
    # slower than it looks, and every send that runs out of window here becomes work for the settle
    # lane and, eventually, a question for the seller. Held well under the stale-intent grace so a
    # send still being confirmed can never look like a stalled one.
    send_verify_window_sec: float = 20.0
    # Pacing knobs. The cap is per marketplace account per hour; the delay pairs are the
    # post-go anti-automation jitter ranges ([min, max] seconds — unattended vs attended).
    # Stored already clamped to the hard ceilings above.
    max_actions_per_hour: int = 12
    reply_delay_sec: tuple = (1.0, 3.0)
    interactive_reply_delay_sec: tuple = (1.0, 3.0)
    # normal|fast. Fast is the live-demo mode: no jitter, cap at ceiling, quiet hours off —
    # it deliberately drops the account-safety disguise. The pacing engine reads this; the
    # stored knob values themselves are untouched, so tuned values survive a round-trip.
    pacing_mode: str = "normal"
    # How often the daemon looks for a new release, and where it looks. The check is one small
    # HTTP GET and it only ever queues a notice — nothing installs itself — so this is about how
    # soon a seller hears, not about load. Null base URL means the published one.
    update_check_interval_sec: float = 86400.0
    update_base_url: str | None = None
    # Negotiation knobs (absent keys leave the engine defaults in force; a style/firmness
    # preset layer may later sit between these and the defaults).
    negotiation_max_counters: int = 2
    negotiation_min_offer_ratio: float = 0.6
    negotiation_lowball_cap: int = 3


def _is_real_number(value: object) -> bool:
    # bool is an int subclass; a JSON true/false is never a valid numeric knob.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_real_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_secure_url(base: object) -> bool:
    """Whether `base` can be fetched without handing its contents to whoever is on the path.

    The one place that decision is made, so config-time and fetch-time validation cannot drift
    apart. The host is parsed rather than prefix-matched: a `startswith("http://127.0.0.1")`
    test also accepts `http://127.0.0.1.evil.test`, a host an attacker registers.
    """
    if not isinstance(base, str):
        return False
    try:
        parsed = urlsplit(base)
        hostname = parsed.hostname
    except ValueError:
        # An unparseable authority (a stray bracket, a non-numeric port) is not a URL we trust.
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and (hostname or "") in _PLAINTEXT_HOSTS


def _require_secure_url(key: str, base: str) -> None:
    if not is_secure_url(base):
        raise ConfigError(f"{key} must be an https URL (plain http only to loopback), got {base!r}")


def _validate(raw: dict) -> Config:
    known = {f.name for f in fields(Config)}
    for key in raw:
        if key not in known:
            log.warning("unknown config key %r ignored", key)

    values: dict = {}

    if "log_level" in raw:
        level = raw["log_level"]
        if not isinstance(level, str) or level.upper() not in _VALID_LOG_LEVELS:
            raise ConfigError(
                f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}, got {level!r}"
            )
        values["log_level"] = level.upper()

    if "tick_interval_sec" in raw:
        tick = raw["tick_interval_sec"]
        if not _is_real_number(tick) or tick <= 0:
            raise ConfigError(f"tick_interval_sec must be a positive number, got {tick!r}")
        values["tick_interval_sec"] = float(tick)

    if "retention_days" in raw:
        days = raw["retention_days"]
        if not _is_real_int(days) or days < 1:
            raise ConfigError(f"retention_days must be an integer >= 1, got {days!r}")
        values["retention_days"] = days

    if "routine_events_retention_hours" in raw:
        hours = raw["routine_events_retention_hours"]
        if not _is_real_int(hours) or hours < 1:
            raise ConfigError(
                f"routine_events_retention_hours must be an integer >= 1, got {hours!r}"
            )
        values["routine_events_retention_hours"] = hours

    if "backups_keep" in raw:
        keep = raw["backups_keep"]
        if not _is_real_int(keep) or keep < 0:
            raise ConfigError(f"backups_keep must be an integer >= 0, got {keep!r}")
        values["backups_keep"] = keep

    if "daemon_mode" in raw:
        mode = raw["daemon_mode"]
        if mode not in _VALID_DAEMON_MODES:
            raise ConfigError(
                f"daemon_mode must be one of {sorted(_VALID_DAEMON_MODES)}, got {mode!r}"
            )
        values["daemon_mode"] = mode

    if "daemon_label" in raw:
        label = raw["daemon_label"]
        if label is not None and not isinstance(label, str):
            raise ConfigError(f"daemon_label must be a string or null, got {label!r}")
        values["daemon_label"] = label

    if "http_port" in raw:
        port = raw["http_port"]
        # 0 is an escape hatch meaning "let the OS choose an ephemeral port" — useful for
        # side-by-side dev daemons and tests. It is not for normal use: generated harness
        # configs pin the port, so an ephemeral one goes stale on restart.
        if not _is_real_int(port) or (port != 0 and not (1024 <= port <= 65535)):
            raise ConfigError(f"http_port must be 0 or an integer in 1024..65535, got {port!r}")
        values["http_port"] = port

    if "pass_deadline_sec" in raw:
        deadline = raw["pass_deadline_sec"]
        if not _is_real_number(deadline) or deadline <= 0:
            raise ConfigError(f"pass_deadline_sec must be a positive number, got {deadline!r}")
        values["pass_deadline_sec"] = float(deadline)

    if "pass_model" in raw:
        model = raw["pass_model"]
        if not isinstance(model, str) or not model.strip():
            raise ConfigError(f"pass_model must be a non-empty string, got {model!r}")
        values["pass_model"] = model.strip()

    if "claude_bin" in raw:
        claude_bin = raw["claude_bin"]
        if claude_bin is not None and (not isinstance(claude_bin, str) or not claude_bin.strip()):
            raise ConfigError(f"claude_bin must be a non-empty string or null, got {claude_bin!r}")
        values["claude_bin"] = claude_bin

    if "node_bin_dir" in raw:
        node_bin_dir = raw["node_bin_dir"]
        if node_bin_dir is not None and (
            not isinstance(node_bin_dir, str) or not node_bin_dir.strip()
        ):
            raise ConfigError(
                f"node_bin_dir must be a directory path or null, got {node_bin_dir!r}"
            )
        values["node_bin_dir"] = node_bin_dir.strip() if node_bin_dir is not None else None

    for key in (
        "carousell_ai_api_base",
        "carousell_ai_web_base_url",
        "telegram_api_base",
        "discord_api_base",
    ):
        if key in raw:
            base = raw[key]
            if not isinstance(base, str) or base != base.strip():
                raise ConfigError(
                    f"{key} must be a URL with no surrounding whitespace, got {base!r}"
                )
            _require_secure_url(key, base)
            values[key] = base.rstrip("/")

    if "chrome_cdp_port" in raw:
        port = raw["chrome_cdp_port"]
        if port is not None and (not _is_real_int(port) or not (1024 <= port <= 65535)):
            raise ConfigError(
                f"chrome_cdp_port must be an integer in 1024..65535 or null, got {port!r}"
            )
        values["chrome_cdp_port"] = port

    if "chrome_bin" in raw:
        chrome_bin = raw["chrome_bin"]
        if chrome_bin is not None and (not isinstance(chrome_bin, str) or not chrome_bin.strip()):
            raise ConfigError(
                f"chrome_bin must be a path to the Chrome executable or null, got {chrome_bin!r}"
            )
        values["chrome_bin"] = chrome_bin.strip() if chrome_bin is not None else None

    if "playwright_mcp_cmd" in raw:
        cmd = raw["playwright_mcp_cmd"]
        if cmd is not None and (
            not isinstance(cmd, list) or not cmd or not all(isinstance(part, str) for part in cmd)
        ):
            raise ConfigError(
                f"playwright_mcp_cmd must be a non-empty list of strings or null, got {cmd!r}"
            )
        values["playwright_mcp_cmd"] = list(cmd) if cmd is not None else None

    if "inbox_read_interval_sec" in raw:
        interval = raw["inbox_read_interval_sec"]
        if not _is_real_number(interval) or interval <= 0:
            raise ConfigError(
                f"inbox_read_interval_sec must be a positive number, got {interval!r}"
            )
        values["inbox_read_interval_sec"] = float(interval)

    if "inbox_full_sweep_every" in raw:
        every = raw["inbox_full_sweep_every"]
        # 1 means every tick is a full sweep (the skip gate disabled) — a supported posture, since
        # the gate is a cost optimization and never a correctness input.
        if not _is_real_int(every) or every < 1:
            raise ConfigError(f"inbox_full_sweep_every must be an integer >= 1, got {every!r}")
        values["inbox_full_sweep_every"] = every

    if "send_verify_window_sec" in raw:
        window = raw["send_verify_window_sec"]
        if not _is_real_number(window) or window < 0:
            raise ConfigError(
                f"send_verify_window_sec must be a non-negative number, got {window!r}"
            )
        values["send_verify_window_sec"] = float(window)

    if "browser_blind_after" in raw:
        blind = raw["browser_blind_after"]
        if not _is_real_int(blind) or blind < 1:
            raise ConfigError(f"browser_blind_after must be an integer >= 1, got {blind!r}")
        values["browser_blind_after"] = blind

    # Pacing knobs: malformed → ConfigError like everything else; well-formed but looser than
    # the hard ceilings → clamped down (tighten-only — see the ceiling constants above).
    if "max_actions_per_hour" in raw:
        cap = raw["max_actions_per_hour"]
        if not _is_real_int(cap) or cap < 1:
            raise ConfigError(
                f"max_actions_per_hour must be an integer >= 1, got {cap!r} "
                "(to stop the agent, pause it instead)"
            )
        values["max_actions_per_hour"] = min(cap, HARD_CAP_CEILING)

    for key in ("reply_delay_sec", "interactive_reply_delay_sec"):
        if key in raw:
            values[key] = _validate_delay_pair(key, raw[key])

    if "pacing_mode" in raw:
        mode = raw["pacing_mode"]
        if mode not in _VALID_PACING_MODES:
            raise ConfigError(
                f"pacing_mode must be one of {sorted(_VALID_PACING_MODES)}, got {mode!r}"
            )
        values["pacing_mode"] = mode

    if "update_check_interval_sec" in raw:
        interval = raw["update_check_interval_sec"]
        if not _is_real_number(interval) or interval <= 0:
            raise ConfigError(
                f"update_check_interval_sec must be a positive number, got {interval!r}"
            )
        values["update_check_interval_sec"] = float(interval)

    if "update_base_url" in raw:
        base = raw["update_base_url"]
        if base is not None:
            if not isinstance(base, str) or base != base.strip():
                raise ConfigError(
                    f"update_base_url must be a URL with no surrounding whitespace or null, "
                    f"got {base!r}"
                )
            _require_secure_url("update_base_url", base)
        values["update_base_url"] = base.rstrip("/") if base else None

    if "negotiation_max_counters" in raw:
        counters = raw["negotiation_max_counters"]
        if not _is_real_int(counters) or counters < 0:
            raise ConfigError(f"negotiation_max_counters must be an integer >= 0, got {counters!r}")
        values["negotiation_max_counters"] = counters

    if "negotiation_min_offer_ratio" in raw:
        ratio = raw["negotiation_min_offer_ratio"]
        if not _is_real_number(ratio) or not (0 < ratio <= 1):
            raise ConfigError(
                f"negotiation_min_offer_ratio must be a number in (0, 1], got {ratio!r}"
            )
        values["negotiation_min_offer_ratio"] = float(ratio)

    if "negotiation_lowball_cap" in raw:
        lowball = raw["negotiation_lowball_cap"]
        if not _is_real_int(lowball) or lowball < 1:
            raise ConfigError(f"negotiation_lowball_cap must be an integer >= 1, got {lowball!r}")
        values["negotiation_lowball_cap"] = lowball

    return Config(**values)


def _validate_delay_pair(key: str, value: object) -> tuple:
    """Validate a [min, max] jitter pair; clamp max (then min) down to the delay ceiling."""
    if not isinstance(value, list) or len(value) != 2 or not all(_is_real_number(v) for v in value):
        raise ConfigError(f"{key} must be a [min, max] pair of numbers, got {value!r}")
    delay_min, delay_max = float(value[0]), float(value[1])
    if delay_min < 0 or delay_max < delay_min:
        raise ConfigError(f"{key} must satisfy 0 <= min <= max, got {value!r}")
    delay_max = min(delay_max, HARD_DELAY_CEILING_SEC)
    delay_min = min(delay_min, delay_max)
    return (delay_min, delay_max)


def load(path: Path | None = None) -> Config:
    """Load config from `path` (default: the canonical config path). Missing file → defaults."""
    target = path if path is not None else paths.config_path()
    try:
        text = target.read_text()
    except FileNotFoundError:
        return Config()
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{target} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{target} must contain a JSON object, got {type(raw).__name__}")
    return _validate(raw)


def merge_into_file(updates: dict, path: Path | None = None) -> None:
    """Merge keys into config.json, preserving the rest. For the installer and tools — NOT the
    daemon, which only ever reads config. Values are validated on the next load()."""
    target = path if path is not None else paths.config_path()
    paths.ensure_config_dir()
    try:
        raw = json.loads(target.read_text())
        if not isinstance(raw, dict):
            raw = {}
    except FileNotFoundError:
        raw = {}
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{target} is not valid JSON: {exc}") from exc
    raw.update(updates)
    target.write_text(json.dumps(raw, indent=2) + "\n")
