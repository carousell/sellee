"""paths.py is the one path authority.

A generator or bind flow that composes its own filesystem location can write to a place the
running daemon never reads (the stale-clone class of bug). The structural defense is a single
module that resolves every path and honors the XDG overrides; nothing else may reach for
Path.home(), expanduser, or an XDG_ environment variable. This scan fails if any module under
src/ other than paths.py does.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

AUTHORITY = "selly_agent/paths.py"

FORBIDDEN = (
    re.compile(r"\bPath\.home\b"),
    re.compile(r"\bos\.path\.expanduser\b"),
    re.compile(r"\.expanduser\b"),
    re.compile(r"\bXDG_[A-Z_]+\b"),
    re.compile(r"\bos\.environ\b.*\bHOME\b"),
    # The Windows spellings of the same thing. Without these the guard would hold on macOS and
    # quietly permit a second path authority to grow on Windows.
    re.compile(r"\bUSERPROFILE\b"),
    re.compile(r"\bLOCALAPPDATA\b"),
    re.compile(r"\bAPPDATA\b"),
)


def _code_lines(source: str) -> dict:
    """The source with comments blanked out, keyed by line number.

    Prose about a platform's variables is not path resolution, and a guard that cannot tell the
    difference gets worked around by rewording rather than by fixing anything. String literals are
    deliberately still scanned: `os.environ["USERPROFILE"]` is one.
    """
    lines = dict(enumerate(source.splitlines(), start=1))
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                start, end = token.start[0], token.end[0]
                for lineno in range(start, end + 1):
                    lines[lineno] = lines[lineno].replace(token.string, "")
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return dict(enumerate(source.splitlines(), start=1))  # unparsable: scan it whole
    return lines


def test_only_paths_module_resolves_home_or_xdg() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC))
        if rel == AUTHORITY:
            continue
        for lineno, line in _code_lines(path.read_text(encoding="utf-8")).items():
            if any(pat.search(line) for pat in FORBIDDEN):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, (
        "home/XDG resolution outside paths.py (route it through paths.py):\n" + "\n".join(offenders)
    )
