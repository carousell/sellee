"""Platform abstraction boundary.

All OS-specific knowledge (launch-agent location, launchd operations) lives behind this
seam so no launchd string leaks into the rest of the daemon. macOS is the only concrete
platform today; Windows raises UnsupportedPlatform (the seam exists so the port is a single
module later).
"""

from __future__ import annotations

import sys

from selly_agent.platform.base import Platform, UnsupportedPlatform


def get_platform() -> Platform:
    if sys.platform == "darwin":
        from selly_agent.platform.macos import MacOSPlatform

        return MacOSPlatform()
    raise UnsupportedPlatform(
        f"{sys.platform!r} is not supported yet (macOS only; Windows is a planned port)"
    )


__all__ = ["Platform", "UnsupportedPlatform", "get_platform"]
