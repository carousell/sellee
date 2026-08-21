"""Secret files in the config dir — 0600 from creation, atomic replace, values never logged.

Two secrets live here: the attended MCP bearer token (generated at first daemon start) and
the carousell.ai guest API key (written by provisioning). Values travel via files and HTTP
headers only — never argv, never stdout, never event payloads. A malformed value (whitespace,
control characters) is rejected, never sanitized: a mangled secret would only fail later,
further from the cause.
"""

from __future__ import annotations

import os
import secrets as _stdlib_secrets
from pathlib import Path

from sellee import paths

_TOKEN_BYTES = 32


class SecretValueError(ValueError):
    """A secret value has a shape that could clobber or leak (whitespace/control chars)."""


def _validate(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise SecretValueError("secret value must be a non-empty string")
    if any(ch.isspace() for ch in value) or not value.isprintable():
        raise SecretValueError("secret value contains whitespace or control characters")
    return value


def write_secret(path: Path, value: str) -> None:
    """Atomically write a secret file with 0600 permissions from creation (no chmod window)."""
    _validate(value)
    paths.ensure_config_dir()
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(value + "\n")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def read_secret(path: Path) -> str | None:
    """The stored value, or None when absent. An empty/blank file reads as absent."""
    try:
        value = path.read_text().strip()
    except FileNotFoundError:
        return None
    return value or None


def ensure_mcp_token() -> str:
    """The attended bearer token, generated on first use and stable thereafter."""
    existing = read_secret(paths.mcp_token_path())
    if existing:
        return existing
    token = _stdlib_secrets.token_urlsafe(_TOKEN_BYTES)
    write_secret(paths.mcp_token_path(), token)
    return token


def rotate_mcp_token() -> str:
    """A fresh attended token, replacing the old one on disk. The caller that holds a live
    server must swap its in-memory copy too (the rotate control route does both)."""
    token = _stdlib_secrets.token_urlsafe(_TOKEN_BYTES)
    write_secret(paths.mcp_token_path(), token)
    return token


def read_mcp_token() -> str | None:
    return read_secret(paths.mcp_token_path())


def read_carousell_ai_api_key() -> str | None:
    return read_secret(paths.carousell_ai_api_key_path())


def write_carousell_ai_api_key(value: str) -> None:
    write_secret(paths.carousell_ai_api_key_path(), value)


def read_telegram_bot_token() -> str | None:
    return read_secret(paths.telegram_bot_token_path())


def write_telegram_bot_token(value: str) -> None:
    write_secret(paths.telegram_bot_token_path(), value)


def read_discord_bot_token() -> str | None:
    return read_secret(paths.discord_bot_token_path())


def write_discord_bot_token(value: str) -> None:
    write_secret(paths.discord_bot_token_path(), value)
