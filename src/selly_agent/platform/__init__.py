"""Platform abstraction boundary.

All OS-specific knowledge (launch-agent location, launchd operations) lives behind this
seam so no launchd string leaks into the rest of the daemon. macOS is the only concrete
host platform today; Windows raises UnsupportedPlatform (the seam exists so the port is a single
module later).

The container profile is selected before the host OS is even consulted: what the daemon runs on
there is decided by the image, not by the kernel underneath it — and the image is a Linux one
whose supervisor is Docker rather than anything `sys.platform` could name.
"""

from __future__ import annotations

import sys

from selly_agent import deployment
from selly_agent.platform.base import Platform, UnsupportedPlatform


def get_platform() -> Platform:
    if deployment.is_container():
        from selly_agent.platform.container import ContainerPlatform

        return ContainerPlatform()
    if sys.platform == "darwin":
        from selly_agent.platform.macos import MacOSPlatform

        return MacOSPlatform()
    raise UnsupportedPlatform(
        f"{sys.platform!r} is not supported yet (macOS only; Windows is a planned port)"
    )


__all__ = ["Platform", "UnsupportedPlatform", "get_platform"]
