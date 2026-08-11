"""Linux platform — a systemd user unit, and the directory the user manager reads at login.

This is the only place systemd knowledge lives, and the unit it renders reproduces what the
macOS plist earns rather than inventing a policy: respawn on a crash but not on a clean exit
(`Restart=on-failure`, so a duplicate start that exits 0 stays exited), a 30s pause between
respawns, the environment pinned into the definition because a supervised job inherits none,
and the two log files redirected to the paths the log commands already read.

`--user` throughout: the unit belongs to the login session, which is also where the Chrome the
agent drives lives. Lingering is deliberately never enabled — a daemon supervised while nobody
is logged in could never reach a browser.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from selly_agent.platform.base import Platform

_DEFAULT_LABEL = "selly-agent"

# Where systemd's per-user manager looks for units, relative to home. Kept as parts rather than
# a string because register() also has to recognise a file that sits there.
_UNIT_DIR_PARTS = (".config", "systemd", "user")


def _escape(value: str) -> str:
    """A value safe to interpolate into a unit file.

    `%` starts a systemd specifier (`%h` is the home directory, `%i` an instance name), so a
    literal one has to be doubled or a path containing it expands into something else entirely.
    """
    return str(value).replace("%", "%%")


def _quote(value: str) -> str:
    """One word, quoted the way systemd's own parser reads it — needed because an install under a
    home directory with a space in it would otherwise be split into two arguments."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{_escape(escaped)}"'


class LinuxPlatform(Platform):
    name = "linux"

    def launch_agents_dir(self, home: Path) -> Path:
        return home.joinpath(*_UNIT_DIR_PARTS)

    def default_label(self) -> str:
        return _DEFAULT_LABEL

    def supervisor_filename(self, label: str) -> str:
        return f"{label}.service"

    def render_supervisor(
        self,
        *,
        label: str,
        program_args: list[str],
        stdout_path: Path,
        stderr_path: Path,
        marker: str,
        environment: dict,
    ) -> str:
        env_lines = "".join(
            f"Environment={_quote(f'{key}={value}')}\n"
            for key, value in sorted(environment.items())
        )
        return (
            f"# {_escape(marker)}\n"
            "[Unit]\n"
            f"Description=Selly marketplace agent ({_escape(label)})\n"
            # Without this a crash loop trips systemd's start rate limiter and the unit is parked
            # in `failed` until somebody runs `reset-failed` by hand — a state launchd's throttle
            # never produces. RestartSec below is the pacing instead.
            "StartLimitIntervalSec=0\n"
            "\n"
            "[Service]\n"
            f"ExecStart={' '.join(_quote(arg) for arg in program_args)}\n"
            "Restart=on-failure\n"
            "RestartSec=30\n"
            f"{env_lines}"
            f"StandardOutput=append:{_escape(stdout_path)}\n"
            f"StandardError=append:{_escape(stderr_path)}\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    # --- systemctl --------------------------------------------------------------------------

    def _systemctl(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def register(self, config_path: Path) -> None:
        """Make the unit known and start it, enabling it only where the mode says to.

        Which of those two it is, is decided by where the file sits — the same trick the plist
        uses. In the unit directory, `enable` writes the wants-symlink that starts it at every
        login; anywhere else, `link` makes it startable by name without ever auto-starting.
        """
        path = Path(config_path).resolve()
        self._systemctl("daemon-reload")
        if path.parent.parts[-len(_UNIT_DIR_PARTS) :] == _UNIT_DIR_PARTS:
            self._systemctl("enable", "--now", path.name)
            return
        # --force so a re-install over an existing link replaces it rather than refusing.
        self._systemctl("link", "--force", str(path))
        self._systemctl("start", path.name)

    def unregister(self, label: str) -> None:
        """Stop the unit and take back both kinds of symlink `register` may have written, so a
        mode flip or an uninstall leaves nothing dangling in the unit directory."""
        self._systemctl("disable", "--now", self.supervisor_filename(label))

    def is_registered(self, label: str) -> bool:
        unit = self.supervisor_filename(label)
        return self._systemctl("is-active", "--quiet", unit).returncode == 0
