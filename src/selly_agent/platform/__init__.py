"""Platform abstraction boundary.

Host-integration knowledge — where a job definition lives, how it is registered, what it is
called — lives behind this seam, so no launchd or Task Scheduler string leaks into the rest of the
daemon. What is deliberately *not* here: anything that has to answer before the host is known to be
supported (the venv layout, the path roots, the instance lock), which resolves in its own portable
module instead.

Linux has no implementation yet, and raises rather than pretending: a systemd user unit is the one
piece missing.
"""

from __future__ import annotations

import sys

from selly_agent.platform.base import Platform, UnsupportedPlatform


def get_platform() -> Platform:
    if sys.platform == "darwin":
        from selly_agent.platform.macos import MacOSPlatform

        return MacOSPlatform()
    if sys.platform == "win32":
        from selly_agent.platform.windows import WindowsPlatform

        return WindowsPlatform()
    raise UnsupportedPlatform(
        f"{sys.platform!r} is not supported yet (macOS and Windows; Linux is a planned port)"
    )


__all__ = ["Platform", "UnsupportedPlatform", "get_platform"]
