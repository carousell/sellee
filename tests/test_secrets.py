"""Secret files: 0600 from creation, atomic, validated, absent-tolerant reads."""

from __future__ import annotations

import stat

import pytest

from sellee import paths, secrets


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_write_and_read_round_trip_with_0600(xdg_tmp) -> None:
    path = paths.mcp_token_path()
    secrets.write_secret(path, "abc123")
    assert secrets.read_secret(path) == "abc123"
    assert _mode(path) == 0o600
    assert not path.with_name(path.name + ".tmp").exists()


def test_read_absent_returns_none(xdg_tmp) -> None:
    assert secrets.read_secret(paths.mcp_token_path()) is None
    assert secrets.read_mcp_token() is None
    assert secrets.read_carousell_ai_api_key() is None


def test_blank_file_reads_as_absent(xdg_tmp) -> None:
    paths.ensure_config_dir()
    paths.mcp_token_path().write_text("  \n")
    assert secrets.read_mcp_token() is None


@pytest.mark.parametrize("bad", ["", "with space", "line\nbreak", "tab\tchar", "ctrl\x00char"])
def test_malformed_values_rejected_never_sanitized(xdg_tmp, bad) -> None:
    with pytest.raises(secrets.SecretValueError):
        secrets.write_secret(paths.mcp_token_path(), bad)
    assert not paths.mcp_token_path().exists()


def test_ensure_mcp_token_generates_once_then_stays_stable(xdg_tmp) -> None:
    token = secrets.ensure_mcp_token()
    assert token
    assert _mode(paths.mcp_token_path()) == 0o600
    assert secrets.ensure_mcp_token() == token


def test_carousell_ai_key_helpers(xdg_tmp) -> None:
    secrets.write_carousell_ai_api_key("key-xyz")
    assert secrets.read_carousell_ai_api_key() == "key-xyz"
    assert _mode(paths.carousell_ai_api_key_path()) == 0o600
