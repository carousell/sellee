"""paths.py is the one path authority.

A generator or bind flow that composes its own filesystem location can write to a place the
running daemon never reads (the stale-clone class of bug). The structural defense is a single
module that resolves every path and honors the XDG overrides; nothing else may reach for
Path.home(), expanduser, or an XDG_ environment variable. This scan fails if any module under
src/ other than paths.py does.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

AUTHORITY = "sellee/paths.py"

FORBIDDEN = (
    re.compile(r"\bPath\.home\b"),
    re.compile(r"\bos\.path\.expanduser\b"),
    re.compile(r"\.expanduser\b"),
    re.compile(r"\bXDG_[A-Z_]+\b"),
    re.compile(r"\bos\.environ\b.*\bHOME\b"),
)


def test_only_paths_module_resolves_home_or_xdg() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC))
        if rel == AUTHORITY:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if any(pat.search(line) for pat in FORBIDDEN):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, (
        "home/XDG resolution outside paths.py (route it through paths.py):\n" + "\n".join(offenders)
    )
