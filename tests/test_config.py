"""Config loads defaults, honors overrides, rejects invalid values, warns on unknown keys."""

from __future__ import annotations

import json
import logging

import pytest

from sellee import paths
from sellee.config import Config, ConfigError, load


def _write_config(obj) -> None:
    paths.ensure_config_dir()
    paths.config_path().write_text(json.dumps(obj))


def test_missing_file_yields_all_defaults(xdg_tmp) -> None:
    cfg = load()
    assert cfg == Config()
    assert cfg.log_level == "INFO"
    assert cfg.tick_interval_sec == 5.0
    assert cfg.routine_events_retention_hours == 24


def test_values_are_read_from_the_config_path(xdg_tmp) -> None:
    _write_config(
        {
            "log_level": "debug",
            "tick_interval_sec": 2,
            "retention_days": 30,
            "routine_events_retention_hours": 6,
            "backups_keep": 3,
        }
    )
    cfg = load()
    assert cfg.log_level == "DEBUG"  # normalized to canonical case
    assert cfg.tick_interval_sec == 2.0
    assert cfg.retention_days == 30
    assert cfg.routine_events_retention_hours == 6
    assert cfg.backups_keep == 3


def test_pass_and_http_knobs_are_read(xdg_tmp) -> None:
    _write_config(
        {
            "http_port": 8123,
            "pass_deadline_sec": 300,
            "pass_model": "opus",
            "claude_bin": "/opt/claude/bin/claude",
            "carousell_ai_api_base": "http://127.0.0.1:9999/",
            "carousell_ai_web_base_url": "http://127.0.0.1:9998",
        }
    )
    cfg = load()
    assert cfg.http_port == 8123
    assert cfg.pass_deadline_sec == 300.0
    assert cfg.pass_model == "opus"
    assert cfg.claude_bin == "/opt/claude/bin/claude"
    assert cfg.carousell_ai_api_base == "http://127.0.0.1:9999"  # trailing slash trimmed
    assert cfg.carousell_ai_web_base_url == "http://127.0.0.1:9998"


def test_claude_bin_defaults_to_null(xdg_tmp) -> None:
    _write_config({"claude_bin": None})
    assert load().claude_bin is None


def test_http_port_zero_allowed_as_ephemeral(xdg_tmp) -> None:
    _write_config({"http_port": 0})
    assert load().http_port == 0


@pytest.mark.parametrize(
    "obj",
    [
        {"log_level": "LOUD"},
        {"tick_interval_sec": 0},
        {"tick_interval_sec": -1},
        {"tick_interval_sec": "fast"},
        {"tick_interval_sec": True},
        {"retention_days": 0},
        {"retention_days": 1.5},
        {"routine_events_retention_hours": 0},
        {"routine_events_retention_hours": 1.5},
        {"backups_keep": -1},
        {"http_port": 80},
        {"http_port": 70000},
        {"http_port": "7355"},
        {"http_port": True},
        {"pass_deadline_sec": 0},
        {"pass_deadline_sec": "long"},
        {"pass_model": ""},
        {"pass_model": 5},
        {"claude_bin": ""},
        {"claude_bin": 5},
        {"carousell_ai_api_base": "api.carousell.ai"},
        {"carousell_ai_api_base": " https://api.carousell.ai"},
        {"carousell_ai_web_base_url": "ftp://x"},
    ],
)
def test_invalid_values_are_rejected_not_sanitized(xdg_tmp, obj) -> None:
    _write_config(obj)
    with pytest.raises(ConfigError):
        load()


@pytest.mark.parametrize(
    "key",
    [
        "carousell_ai_api_base",
        "carousell_ai_web_base_url",
        "telegram_api_base",
        "discord_api_base",
        "update_base_url",
    ],
)
def test_a_cleartext_base_to_a_real_host_is_refused(xdg_tmp, key) -> None:
    # The Telegram and Discord bases carry the bot token in the URL path and the update base
    # serves code this machine executes, so cleartext off-box is refused for every URL knob.
    _write_config({key: "http://bases.example.test"})
    with pytest.raises(ConfigError):
        load()


def test_http_to_loopback_is_allowed_but_the_host_is_parsed_not_prefix_matched(xdg_tmp) -> None:
    _write_config(
        {
            "carousell_ai_api_base": "http://127.0.0.1:9999",
            "carousell_ai_web_base_url": "http://localhost:9998",
            "telegram_api_base": "http://127.0.0.1:9997",
            "discord_api_base": "http://[::1]:9996",
            "update_base_url": "http://127.0.0.1:9995",
        }
    )
    cfg = load()
    assert cfg.carousell_ai_api_base == "http://127.0.0.1:9999"
    assert cfg.carousell_ai_web_base_url == "http://localhost:9998"
    assert cfg.telegram_api_base == "http://127.0.0.1:9997"
    assert cfg.discord_api_base == "http://[::1]:9996"
    assert cfg.update_base_url == "http://127.0.0.1:9995"

    # A host an attacker owns that merely starts with the loopback literal: a prefix test on
    # "http://127.0.0.1" would let this through, parsing the hostname does not.
    _write_config({"telegram_api_base": "http://127.0.0.1.evil.test/bot"})
    with pytest.raises(ConfigError):
        load()


def test_pacing_and_negotiation_defaults(xdg_tmp) -> None:
    cfg = load()
    assert cfg.max_actions_per_hour == 12
    assert cfg.reply_delay_sec == (1.0, 3.0)
    assert cfg.interactive_reply_delay_sec == (1.0, 3.0)
    assert cfg.pacing_mode == "normal"
    assert cfg.negotiation_max_counters == 2
    assert cfg.negotiation_min_offer_ratio == 0.6
    assert cfg.negotiation_lowball_cap == 3


def test_pacing_and_negotiation_knobs_are_read(xdg_tmp) -> None:
    _write_config(
        {
            "max_actions_per_hour": 6,
            "reply_delay_sec": [0, 2],
            "interactive_reply_delay_sec": [0.5, 1.5],
            "pacing_mode": "fast",
            "negotiation_max_counters": 4,
            "negotiation_min_offer_ratio": 0.5,
            "negotiation_lowball_cap": 2,
        }
    )
    cfg = load()
    assert cfg.max_actions_per_hour == 6
    assert cfg.reply_delay_sec == (0.0, 2.0)
    assert cfg.interactive_reply_delay_sec == (0.5, 1.5)
    assert cfg.pacing_mode == "fast"
    assert cfg.negotiation_max_counters == 4
    assert cfg.negotiation_min_offer_ratio == 0.5
    assert cfg.negotiation_lowball_cap == 2


def test_valid_but_loose_pacing_values_clamp_down(xdg_tmp) -> None:
    # Tighten-only: a well-formed cap/delay above the hard ceiling clamps down (never rejects,
    # never relaxes) — distinct from malformed values, which reject below.
    _write_config(
        {
            "max_actions_per_hour": 500,
            "reply_delay_sec": [10, 120],
            "interactive_reply_delay_sec": [0, 60],
        }
    )
    cfg = load()
    assert cfg.max_actions_per_hour == 60
    assert cfg.reply_delay_sec == (3.0, 3.0)  # min follows max down so min <= max holds
    assert cfg.interactive_reply_delay_sec == (0.0, 3.0)


@pytest.mark.parametrize(
    "obj",
    [
        {"max_actions_per_hour": 0},
        {"max_actions_per_hour": -1},
        {"max_actions_per_hour": "12"},
        {"max_actions_per_hour": True},
        {"reply_delay_sec": [3]},
        {"reply_delay_sec": [-1, 2]},
        {"reply_delay_sec": [2, 1]},
        {"reply_delay_sec": "fast"},
        {"reply_delay_sec": [1, "2"]},
        {"interactive_reply_delay_sec": [2, 1]},
        {"pacing_mode": "FAST"},
        {"pacing_mode": "turbo"},
        {"pacing_mode": 1},
        {"negotiation_max_counters": -1},
        {"negotiation_max_counters": 1.5},
        {"negotiation_min_offer_ratio": 0},
        {"negotiation_min_offer_ratio": 1.2},
        {"negotiation_min_offer_ratio": "0.6"},
        {"negotiation_lowball_cap": 0},
    ],
)
def test_invalid_pacing_and_negotiation_values_are_rejected(xdg_tmp, obj) -> None:
    _write_config(obj)
    with pytest.raises(ConfigError):
        load()


@pytest.mark.parametrize(
    "obj",
    [
        {"chrome_cdp_port": 80},
        {"chrome_cdp_port": 70000},
        {"chrome_cdp_port": "9222"},
        {"playwright_mcp_cmd": []},
        {"playwright_mcp_cmd": "npx @playwright/mcp"},
        {"playwright_mcp_cmd": ["npx", 1]},
        {"inbox_read_interval_sec": 0},
        {"inbox_read_interval_sec": -5},
        {"inbox_full_sweep_every": 0},
        {"inbox_full_sweep_every": 1.5},
        {"browser_blind_after": 0},
        {"chrome_bin": ""},
        {"chrome_bin": "   "},
        {"chrome_bin": 5},
    ],
)
def test_invalid_browser_values_are_rejected(xdg_tmp, obj) -> None:
    _write_config(obj)
    with pytest.raises(ConfigError):
        load()


def test_an_unset_chrome_port_means_chrome_chooses_one(xdg_tmp) -> None:
    """The default. A fixed port is one a local process can bind before Chrome does; letting Chrome
    choose leaves nothing to bind ahead of it."""
    assert Config().chrome_cdp_port is None
    _write_config({"chrome_cdp_port": None})
    assert load().chrome_cdp_port is None


def test_browser_knobs_round_trip(xdg_tmp) -> None:
    _write_config(
        {
            "chrome_cdp_port": 9333,
            "playwright_mcp_cmd": ["node", "/opt/mcp/cli.js"],
            "inbox_read_interval_sec": 120,
            "inbox_full_sweep_every": 1,
            "browser_blind_after": 5,
            "chrome_bin": " /opt/chrome/chrome ",
        }
    )
    cfg = load()
    assert cfg.chrome_bin == "/opt/chrome/chrome"
    assert cfg.chrome_cdp_port == 9333  # pinning still works, for the container and the by-hand run
    assert cfg.playwright_mcp_cmd == ["node", "/opt/mcp/cli.js"]
    assert cfg.inbox_read_interval_sec == 120.0
    assert cfg.inbox_full_sweep_every == 1  # 1 disables the skip gate, a supported posture
    assert cfg.browser_blind_after == 5


def test_a_null_playwright_command_means_resolve_npx_at_spawn(xdg_tmp) -> None:
    _write_config({"playwright_mcp_cmd": None})
    assert load().playwright_mcp_cmd is None


def test_unknown_keys_warn_and_are_ignored(xdg_tmp, caplog) -> None:
    _write_config({"tick_interval_sec": 7, "future_knob": "whatever"})
    with caplog.at_level(logging.WARNING):
        cfg = load()
    assert cfg.tick_interval_sec == 7.0
    assert any("future_knob" in rec.message for rec in caplog.records)


def test_non_object_json_is_rejected(xdg_tmp) -> None:
    _write_config([1, 2, 3])
    with pytest.raises(ConfigError):
        load()
