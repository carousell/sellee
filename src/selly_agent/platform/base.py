"""The OS-abstraction interface. Concrete platforms implement it; callers depend only here."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class UnsupportedPlatform(Exception):
    """Raised by get_platform() when the host OS has no implementation yet."""


class Platform(ABC):
    """OS-specific operations the daemon needs. Keep this surface small.

    `home` is always passed in by paths.py rather than resolved here: home/XDG resolution is
    the sole responsibility of paths.py, so this module never reaches for it directly.
    """

    name: str = "base"

    # What render_supervisor's text must be written as. Not cosmetic: the tool that imports a
    # Windows task definition rejects one that is not UTF-16.
    definition_encoding: str = "utf-8"

    @abstractmethod
    def launch_agents_dir(self, home: Path) -> Path:
        """The per-user auto-start directory the supervisor reads at login."""

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
        environment: dict,
        start_at_login: bool,
    ) -> str:
        """Render the job definition (carries the marker so ours-vs-foreign is decidable).

        `environment` is pinned into the definition: a supervised job inherits nothing from the
        shell that installed it, so anything the daemon must see (XDG overrides) goes here.

        `start_at_login` is asked because not every supervisor can express it by *where* the
        definition is put — a registered scheduled task persists across logins wherever its XML
        came from, so it has to say so in the definition itself."""

    @abstractmethod
    def register(self, config_path: Path) -> None:
        """Register + start the job from its config file (crash keep-alive active thereafter)."""

    @abstractmethod
    def unregister(self, label: str) -> None:
        """Unregister the job; it stays stopped until re-registered."""

    @abstractmethod
    def is_registered(self, label: str) -> bool:
        """True if the supervisor currently has the job loaded."""
