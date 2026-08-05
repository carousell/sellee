"""The shape a probe answers in, shared by the preflight gates and the healthcheck.

Both are the same pattern and it is the one worth copying from the legacy installer: a *pure*
status function that decides an outcome from inputs, wrapped by an IO shim that can never throw.
A probe that crashes takes the whole summary with it, and a summary that dies on its third line
is worse than useless — it hides the two checks that already passed and the two that never ran.

What a caller does with a result is the caller's business: preflight dies or offers to install,
the healthcheck renders a line and sets an exit code. Neither decision lives here.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

OK = "ok"
WARN = "warn"
FAIL = "fail"

_GLYPHS = {OK: "✅", WARN: "⚠️", FAIL: "❌"}
_ASCII_GLYPHS = {OK: "[ok]", WARN: "[warn]", FAIL: "[FAIL]"}


def glyph(status: str, stream=None) -> str:
    """The status mark, degraded to ASCII where the stream cannot encode it.

    A legacy Windows console — or output redirected under its code page — does not render the
    emoji as something worse; it raises UnicodeEncodeError partway through the line.
    """
    return _degrade(_GLYPHS.get(status, "•"), _ASCII_GLYPHS.get(status, "*"), stream)


def _degrade(text: str, fallback: str, stream=None) -> str:
    target = sys.stdout if stream is None else stream
    encoding = getattr(target, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return fallback
    return text


@dataclass(frozen=True)
class Check:
    """One probe's answer: what was checked, how it went, and how to fix it if it did not."""

    name: str
    status: str
    detail: str
    fix: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    def render(self) -> list:
        """The check as display lines: a glyph line, plus a fix line when there is one to give."""
        lines = [f"{glyph(self.status)} {self.name}: {self.detail}"]
        if self.fix and self.status != OK:
            lines.append(f"   {_degrade('→', '->')} {self.fix}")
        return lines


def ok(name: str, detail: str) -> Check:
    return Check(name=name, status=OK, detail=detail)


def warn(name: str, detail: str, fix: str = "") -> Check:
    return Check(name=name, status=WARN, detail=detail, fix=fix)


def fail(name: str, detail: str, fix: str = "") -> Check:
    return Check(name=name, status=FAIL, detail=detail, fix=fix)


def fail_open(name: str, probe, fix: str = "") -> Check:
    """Run a probe that touches the world, turning any unexpected failure into a failed check.

    Deliberately catches broadly: the point is that no probe can end the run. A caught exception
    is logged at debug and named in the detail, so a genuine bug here is diagnosable rather than
    silently rendered as a plain failure.
    """
    try:
        return probe()
    except Exception as exc:  # noqa: BLE001 — a probe must never take down the summary
        log.debug("probe %r raised", name, exc_info=True)
        return fail(name, f"could not be checked ({type(exc).__name__}: {exc})", fix)


def render(checks) -> list:
    lines = []
    for check in checks:
        lines.extend(check.render())
    return lines


def exit_code(checks) -> int:
    """0 when nothing failed, 1 when anything did. A warning is not a failure — an optional
    thing being absent is a fact to report, not a reason to fail a script."""
    return 1 if any(check.failed for check in checks) else 0
