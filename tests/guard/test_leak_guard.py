"""Leak guard — no real credential-shaped literal may land in a repo file.

A pure allowlist: the known-real secret values are never encoded here (that would re-leak
them). Anything matching a credential shape that is not on the fake allowlist fails, and the
failure names only file:line — never the matched literal. Walks the repo tree (excluding VCS,
virtualenv, and cache dirs) so it is meaningful under jj, git, or an unpacked tarball alike.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Approved FAKE literals used in docs / fixtures. Real values never appear here.
APPROVED_FAKE_IDS = {
    "123456789",  # fake telegram bot id / chat id
    "6591234567",  # classic SG placeholder
}

# Telegram bot token shape: 8-12 digit bot id, colon, "AA..." auth part.
TOKEN_RE = re.compile(r"\b\d{8,12}:AA[A-Za-z0-9_-]{25,}")
# Long digit runs on lines that mention a chat/offset identifier.
DIGIT_RUN_RE = re.compile(r"\d{6,}")
IDENTIFIER_LINE_RE = re.compile(r"chat|update_offset")

SELF = Path(__file__).resolve()
# Directories that are not our source: VCS metadata, virtualenvs, build/test caches, and the
# scratch tree. `.local` is excluded from version control and is where manual install sessions
# put their data root — so it holds real tokens and real page dumps *on purpose*, and scanning
# it turns a live install test into a failing suite for everyone afterwards.
_EXCLUDED_DIRS = {
    ".git",
    ".jj",
    ".local",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "node_modules",
}


def _is_excluded(path: Path) -> bool:
    for part in path.relative_to(ROOT).parts:
        if part in _EXCLUDED_DIRS or part.startswith(".venv") or part.endswith(".egg-info"):
            return True
    return False


def _repo_text_files() -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or _is_excluded(path):
            continue
        if path.resolve() == SELF:  # self-exclude: our own fixtures are bad examples by design
            continue
        try:
            files.append((path, path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue  # binary / unreadable
    return files


def test_leak_guard_actually_scans_files() -> None:
    # guard against the scan silently becoming a no-op (the vacuous-pass trap)
    assert len(_repo_text_files()) > 5


def test_no_bot_token_shape_in_repo_files() -> None:
    offenders: list[str] = []
    for path, text in _repo_text_files():
        for lineno, line in enumerate(text.splitlines(), start=1):
            if TOKEN_RE.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, "bot-token shape in repo files:\n" + "\n".join(offenders)


def test_no_unapproved_identifier_digits_in_repo_files() -> None:
    offenders: list[str] = []
    for path, text in _repo_text_files():
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not IDENTIFIER_LINE_RE.search(line):
                continue
            for match in DIGIT_RUN_RE.findall(line):
                if match not in APPROVED_FAKE_IDS:
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
                    break
    assert not offenders, "unapproved identifier digits in repo files:\n" + "\n".join(offenders)
