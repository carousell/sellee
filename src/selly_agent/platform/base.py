"""The OS-abstraction interface. Concrete platforms implement it; callers depend only here."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class UnsupportedPlatform(Exception):
    """Raised by get_platform() when the host OS has no implementation yet."""


class ImageToolUnavailable(Exception):
    """The platform's image converter is missing or failed. Its message names the file."""


class Platform(ABC):
    """OS-specific operations the daemon needs. Keep this surface small.

    `home` is always passed in by paths.py rather than resolved here: home/XDG resolution is
    the sole responsibility of paths.py, so this module never reaches for it directly.
    """

    name: str = "base"

    @abstractmethod
    def launch_agents_dir(self, home: Path) -> Path:
        """The per-user auto-start directory the supervisor reads at login."""

    # --- images ----------------------------------------------------------------------------

    @abstractmethod
    def to_jpeg(self, src: Path, dest: Path, max_dim: int) -> None:
        """Write src to dest as a JPEG no larger than max_dim on its longest side.

        The runtime is stdlib-only and stdlib cannot transform images, so this shells out to
        whatever the OS ships. Raises ImageToolUnavailable — naming the file — when the tool is
        absent or the conversion fails, so the caller can escalate something actionable rather
        than upload a file the marketplace will reject.
        """

    # --- supervisor (keep-alive job) ------------------------------------------------------

    @abstractmethod
    def default_label(self) -> str:
        """The default supervisor job label."""

    @abstractmethod
    def supervisor_filename(self, label: str) -> str:
        """The on-disk filename for a job config with this label."""

    @abstractmethod
    def render_supervisor(
        self,
        *,
        label: str,
        program_args: list[str],
        stdout_path: Path,
        stderr_path: Path,
        marker: str,
    ) -> str:
        """Render the job definition (carries the marker so ours-vs-foreign is decidable)."""

    @abstractmethod
    def register(self, config_path: Path) -> None:
        """Register + start the job from its config file (crash keep-alive active thereafter)."""

    @abstractmethod
    def unregister(self, label: str) -> None:
        """Unregister the job; it stays stopped until re-registered."""

    @abstractmethod
    def is_registered(self, label: str) -> bool:
        """True if the supervisor currently has the job loaded."""
