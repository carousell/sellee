"""Platform abstraction boundary.

All OS-specific knowledge (where the auto-start directory is, how a supervisor is driven) lives
behind this seam, so no launchd or systemd string leaks into the rest of the daemon. macOS and
Linux are the concrete host platforms; Windows raises UnsupportedPlatform (the seam exists so
the port is a single module later).

The container profile is selected before the host OS is even consulted: what the daemon runs on
there is decided by the image, not by the kernel underneath it — and the image is a Linux one
whose supervisor is Docker rather than anything `sys.platform` could name.
"""

from __future__ import annotations

import sys

from sellee import deployment
from sellee.platform.base import Platform, UnsupportedPlatform


def get_platform() -> Platform:
    if deployment.is_container():
        from sellee.platform.container import ContainerPlatform

        return ContainerPlatform()
    if sys.platform == "darwin":
        from sellee.platform.macos import MacOSPlatform

        return MacOSPlatform()
    if sys.platform == "linux":
        from sellee.platform.linux import LinuxPlatform

        return LinuxPlatform()
    raise UnsupportedPlatform(
        f"{sys.platform!r} is not supported yet (macOS and Linux; Windows is a planned port)"
    )


__all__ = ["Platform", "UnsupportedPlatform", "get_platform"]
