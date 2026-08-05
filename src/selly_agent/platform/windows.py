"""Windows platform — a per-user scheduled task as the supervisor.

This is the only place Task Scheduler knowledge lives. The job is to reproduce what launchd gives
us on macOS, which Task Scheduler does not offer directly:

  * **Start at login** is a LogonTrigger, the one part that maps cleanly.
  * **Keep-alive** has no equivalent — a task's restart-on-failure counters are bounded and keyed
    to exit codes, and the daemon exits 0 both when it is asked to stop and when it finds another
    instance already running. So the keep-alive is a repetition trigger instead: every five
    minutes the task is started again, and a start against a live daemon exits 0 immediately
    because the instance lock is already held. Duplicates are free, which turns "run it again
    forever" into a watchdog. MultipleInstances=IgnoreNew keeps Task Scheduler from queueing them.
  * **Ownership** cannot be a marker in a file we wrote, because a registered task is not a file
    we own. The marker goes in the task's Description, which is where a person looking at the task
    in the UI would want it anyway.
  * **Log files** are the daemon's own problem here. Task Scheduler does no output redirection,
    where launchd takes StandardOutPath and StandardErrorPath.

The action names pythonw.exe rather than python.exe: the console-bearing interpreter would flash a
window on the desktop every time the keep-alive trigger fired.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

from selly_agent.platform.base import Platform

_DEFAULT_LABEL = "SellyAgent"

# How often the keep-alive trigger fires. Long enough that a crash loop cannot become a storm (the
# instance lock makes duplicates cheap, not free), short enough that a daemon killed while the
# seller is away is back before they notice.
_KEEPALIVE_MINUTES = 5

# Task Scheduler's own schema. The version is what decides which elements are accepted, and 1.2 is
# the floor for everything used here; it exists on every supported Windows.
_TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


class WindowsPlatform(Platform):
    name = "windows"
    definition_encoding = "utf-16"

    def launch_agents_dir(self, home: Path) -> Path:
        """Where a task definition is kept before it is registered.

        Not a system location: Windows has no directory that means "start this at login" the way
        ~/Library/LaunchAgents does — registration is an API call, not a file placement. The XML
        still has to live somewhere for the register call to read and for the mode logic to find,
        so it goes beside the config, and mode is expressed by the triggers instead.
        """
        return home / ".selly-agent" / "tasks"

    def default_label(self) -> str:
        return _DEFAULT_LABEL

    def supervisor_filename(self, label: str) -> str:
        return f"{label}.xml"

    def render_supervisor(
        self,
        *,
        label: str,
        program_args: list[str],
        stdout_path: Path,
        stderr_path: Path,
        marker: str,
        environment: dict,
        start_at_login: bool = True,
    ) -> str:
        """The task definition. UTF-16 is what schtasks expects on import, so the caller writes
        this text with that encoding; the declaration says so.

        stdout_path and stderr_path are accepted and not used: Task Scheduler cannot redirect
        output, so the daemon opens its own log files. They stay in the signature because the
        supervisor asks every platform the same question.
        """
        interpreter, *arguments = [str(part) for part in program_args]
        argument_line = " ".join(_quote(argument) for argument in arguments)
        working_dir = str(Path(interpreter).parent)
        return (
            '<?xml version="1.0" encoding="UTF-16"?>\n'
            f'<Task version="1.2" xmlns="{_TASK_NAMESPACE}">\n'
            "  <RegistrationInfo>\n"
            f"    <Description>{escape(marker)}</Description>\n"
            "  </RegistrationInfo>\n"
            "  <Triggers>\n"
            # Manual mode leaves this present but disabled rather than absent, so that flipping
            # modes is a re-import of the same shape and `daemon status` reads one thing.
            "    <LogonTrigger>\n"
            f"      <Enabled>{'true' if start_at_login else 'false'}</Enabled>\n"
            "    </LogonTrigger>\n"
            # The keep-alive. A repetition that never ends, on a trigger that is always due.
            "    <TimeTrigger>\n"
            "      <StartBoundary>2020-01-01T00:00:00</StartBoundary>\n"
            "      <Repetition>\n"
            f"        <Interval>PT{_KEEPALIVE_MINUTES}M</Interval>\n"
            "        <StopAtDurationEnd>false</StopAtDurationEnd>\n"
            "      </Repetition>\n"
            "      <Enabled>true</Enabled>\n"
            "    </TimeTrigger>\n"
            "  </Triggers>\n"
            "  <Principals>\n"
            '    <Principal id="Author">\n'
            "      <LogonType>InteractiveToken</LogonType>\n"
            "      <RunLevel>LeastPrivilege</RunLevel>\n"
            "    </Principal>\n"
            "  </Principals>\n"
            "  <Settings>\n"
            # A second start while one is running is dropped rather than queued: the daemon would
            # exit 0 on the instance lock anyway, and a queue would turn a slow start into a pile.
            "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
            "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
            "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
            "    <AllowHardTerminate>true</AllowHardTerminate>\n"
            "    <StartWhenAvailable>true</StartWhenAvailable>\n"
            "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
            "    <IdleSettings>\n"
            "      <StopOnIdleEnd>false</StopOnIdleEnd>\n"
            "      <RestartOnIdle>false</RestartOnIdle>\n"
            "    </IdleSettings>\n"
            "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
            "    <Enabled>true</Enabled>\n"
            "    <Hidden>false</Hidden>\n"
            "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
            # A daemon is meant to run forever, and the default is three days.
            "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
            "    <Priority>7</Priority>\n"
            "  </Settings>\n"
            '  <Actions Context="Author">\n'
            "    <Exec>\n"
            f"      <Command>{escape(interpreter)}</Command>\n"
            f"      <Arguments>{escape(argument_line)}</Arguments>\n"
            f"      <WorkingDirectory>{escape(working_dir)}</WorkingDirectory>\n"
            "    </Exec>\n"
            "  </Actions>\n"
            "</Task>\n"
        )

    # --- schtasks -------------------------------------------------------------------------

    def _schtasks(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["schtasks", *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def register(self, config_path: Path) -> None:
        """Import the definition and start it now, which is what bootstrapping a plist does.

        /F replaces a task of the same name, so a re-install or a mode flip is one call rather
        than delete-then-create with a window in between where nothing is registered.
        """
        label = Path(config_path).stem
        self._schtasks("/Create", "/TN", label, "/XML", str(config_path), "/F")
        self._schtasks("/Run", "/TN", label)

    def unregister(self, label: str) -> None:
        self._schtasks("/Delete", "/TN", label, "/F")

    def is_registered(self, label: str) -> bool:
        return self._schtasks("/Query", "/TN", label).returncode == 0


def _quote(argument: str) -> str:
    """Quote one argument for the task's single Arguments string.

    Task Scheduler stores the arguments as one line and lets the process split them again, so a
    path with a space in it — which is most of them under C:\\Users — has to survive that round
    trip. Only quoting is needed: a Windows path cannot contain a double quote.
    """
    return f'"{argument}"' if " " in argument else argument
