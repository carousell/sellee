"""macOS platform — launchd supervisor and its per-user auto-start directory.

This is the only place launchd knowledge lives. The plist carries the legacy plist's earned
wisdom: RunAtLoad, KeepAlive on non-clean exit only (a clean duplicate/stop exit is not
respawned), and a raised ThrottleInterval so even a crash loop backs off to once per 30s.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from xml.sax.saxutils import escape

from sellee.platform.base import Platform

# How long `unregister` waits for launchctl to finish letting go of a job. `bootout` is
# asynchronous, and a job that still answers `print` is a job a following `start` will decline to
# start. Seconds, because this is teardown of a local process, not a network call — and bounded,
# because a launchctl that will not release is better reported by the next command failing than by
# this one hanging silently.
_BOOTOUT_TIMEOUT_SEC = 10.0
_BOOTOUT_POLL_SEC = 0.1

_DEFAULT_LABEL = "com.sellee.agent"


class MacOSPlatform(Platform):
    name = "macos"

    def launch_agents_dir(self, home: Path) -> Path:
        return home / "Library" / "LaunchAgents"

    def default_label(self) -> str:
        return _DEFAULT_LABEL

    def supervisor_filename(self, label: str) -> str:
        return f"{label}.plist"

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
        args_xml = "\n".join(f"    <string>{escape(a)}</string>" for a in program_args)
        env_xml = ""
        if environment:
            rows = "\n".join(
                f"    <key>{escape(key)}</key>\n    <string>{escape(str(value))}</string>"
                for key, value in sorted(environment.items())
            )
            env_xml = f"  <key>EnvironmentVariables</key>\n  <dict>\n{rows}\n  </dict>\n"
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            f"<!-- {escape(marker)} -->\n"
            '<plist version="1.0">\n'
            "<dict>\n"
            "  <key>Label</key>\n"
            f"  <string>{escape(label)}</string>\n"
            "  <key>ProgramArguments</key>\n"
            "  <array>\n"
            f"{args_xml}\n"
            "  </array>\n"
            "  <key>RunAtLoad</key>\n"
            "  <true/>\n"
            "  <key>KeepAlive</key>\n"
            "  <dict>\n"
            "    <key>SuccessfulExit</key>\n"
            "    <false/>\n"
            "  </dict>\n"
            "  <key>ThrottleInterval</key>\n"
            "  <integer>30</integer>\n"
            f"{env_xml}"
            "  <key>StandardOutPath</key>\n"
            f"  <string>{escape(str(stdout_path))}</string>\n"
            "  <key>StandardErrorPath</key>\n"
            f"  <string>{escape(str(stderr_path))}</string>\n"
            "</dict>\n"
            "</plist>\n"
        )

    # --- launchctl ------------------------------------------------------------------------

    def _domain(self) -> str:
        return f"gui/{os.getuid()}"

    def _launchctl(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["launchctl", *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def register(self, config_path: Path) -> None:
        self._launchctl("bootstrap", self._domain(), str(config_path))

    def unregister(self, label: str) -> None:
        """Take the job out, and do not return until launchctl agrees it is out.

        `bootout` returns before the job is actually gone. Nothing here noticed until a
        `daemon stop` was followed straight away by a `daemon start`: start asks
        `is_registered`, the half-torn-down job still answered yes, so start printed "already
        running" and did nothing — and then the bootout finished. Left with no job and no
        process, on a machine whose heartbeat file was still fresh enough for `status` to report
        it running for another half a minute.

        Waited for here rather than in `supervisor.start`, because the contract this breaks is
        this method's own — "it stays stopped until re-registered" — and every caller of it
        (stop, uninstall, a re-install over an existing job, a mode flip) inherits the same race.

        A deadline rather than a loop without one: if launchctl will not let go, the caller is
        better off proceeding and failing visibly than hanging with no output.
        """
        self._launchctl("bootout", f"{self._domain()}/{label}")
        deadline = time.monotonic() + _BOOTOUT_TIMEOUT_SEC
        while self.is_registered(label) and time.monotonic() < deadline:
            time.sleep(_BOOTOUT_POLL_SEC)

    def is_registered(self, label: str) -> bool:
        return self._launchctl("print", f"{self._domain()}/{label}").returncode == 0
