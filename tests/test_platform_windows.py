"""The Windows supervisor: the task definition, and the schtasks calls around it.

The definition is golden-tested and parsed back, the way the plist is. Nothing here runs
schtasks — the calls are recorded — because what a real Task Scheduler does with this XML is a
live-verification item on the PC, not something a suite on any machine can answer.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

import pytest

from selly_agent import supervisor
from selly_agent.platform.base import RegistrationError
from selly_agent.platform.windows import _TASK_NAMESPACE, WindowsPlatform

GOLDEN = Path(__file__).resolve().parent / "golden" / "SellyAgent.xml"

PROGRAM_ARGS = [
    r"C:\Users\seller\AppData\Local\selly-agent\share\current\.venv\Scripts\pythonw.exe",
    r"C:\Users\seller\AppData\Local\selly-agent\share\current\bin\selly-agent",
    "--env-file",
    r"C:\Users\seller\AppData\Local\selly-agent\config\SellyAgent.env.json",
    "daemon",
    "run",
]

WORKING_DIR = Path(r"C:\Users\seller\AppData\Local\selly-agent\share\current")


def render(**overrides) -> str:
    defaults = {
        "label": "SellyAgent",
        "program_args": PROGRAM_ARGS,
        "stdout_path": Path(r"C:\logs\agent.out.log"),
        "stderr_path": Path(r"C:\logs\agent.err.log"),
        "marker": supervisor.MARKER,
        "environment": {},
        "start_at_login": True,
        "working_dir": WORKING_DIR,
    }
    defaults.update(overrides)
    return WindowsPlatform().render_supervisor(**defaults)


def parse(text: str):
    return ElementTree.fromstring(text)


def find(tree, path: str):
    """An element by path, with the task schema's namespace applied to every step of it."""
    return tree.find("/".join(f"{{{_TASK_NAMESPACE}}}{part}" for part in path.split("/")))


def test_render_matches_golden() -> None:
    assert render() == GOLDEN.read_text(encoding="utf-16")


def test_the_definition_is_well_formed_xml() -> None:
    """It is imported by another program, so "looks right" is not the bar."""
    assert parse(render()) is not None


def test_the_marker_is_the_description_because_a_task_is_not_a_file() -> None:
    """Ours-vs-foreign has to be decidable after registration, when the XML we wrote is no longer
    what Task Scheduler is reading from."""
    description = find(parse(render()), "RegistrationInfo/Description")
    assert description is not None
    assert description.text == supervisor.MARKER


def test_the_keepalive_repeats_forever_and_never_queues() -> None:
    """This is the whole keep-alive: start it again every few minutes, and let the instance lock
    turn the duplicate into an immediate clean exit. A queue would pile them up instead."""
    tree = parse(render())
    assert find(tree, "Triggers/TimeTrigger/Repetition/Interval").text == "PT5M"
    assert find(tree, "Triggers/TimeTrigger/Repetition/StopAtDurationEnd").text == "false"
    assert find(tree, "Settings/MultipleInstancesPolicy").text == "IgnoreNew"


def test_the_daemon_is_not_stopped_for_running_too_long() -> None:
    """The default execution time limit is three days, and this is meant to run forever."""
    assert find(parse(render()), "Settings/ExecutionTimeLimit").text == "PT0S"


def test_a_laptop_on_battery_still_runs_the_agent() -> None:
    tree = parse(render())
    assert find(tree, "Settings/DisallowStartIfOnBatteries").text == "false"
    assert find(tree, "Settings/StopIfGoingOnBatteries").text == "false"


def test_manual_mode_disables_the_logon_trigger() -> None:
    """A registered task persists across logins wherever its XML came from, so "manual" cannot be
    expressed by where the file was put the way it is on macOS."""
    trigger = "Triggers/LogonTrigger/Enabled"
    assert find(parse(render(start_at_login=False)), trigger).text == "false"
    assert find(parse(render(start_at_login=True)), trigger).text == "true"


def test_the_job_runs_without_elevation_in_the_desktop_session() -> None:
    """It drives the seller's own browser, so it belongs to their session; and asking for
    elevation would put a consent prompt in front of a background job."""
    tree = parse(render())
    assert find(tree, "Principals/Principal/LogonType").text == "InteractiveToken"
    assert find(tree, "Principals/Principal/RunLevel").text == "LeastPrivilege"


def test_the_action_names_the_interpreter_and_quotes_what_needs_it() -> None:
    """Task Scheduler stores the arguments as one line and lets the process split them again, so a
    path under C:\\Users — which usually has a space in it — has to survive the round trip."""
    tree = parse(render(program_args=[r"C:\py\pythonw.exe", r"C:\Program Files\a\selly", "daemon"]))
    assert find(tree, "Actions/Exec/Command").text == r"C:\py\pythonw.exe"
    assert find(tree, "Actions/Exec/Arguments").text == r'"C:\Program Files\a\selly" daemon'


def test_the_working_directory_is_what_the_caller_chose() -> None:
    """Chosen, not derived: deriving it from the interpreter path would parse a Windows path with
    the host's rules — which reads C:\\... as one component on every other OS, so the suite would
    pin one thing here and the PC would render another."""
    tree = parse(render(working_dir=Path(r"C:\some\dir")))
    assert find(tree, "Actions/Exec/WorkingDirectory").text == r"C:\some\dir"


def test_the_action_carries_the_environment_file_flag() -> None:
    """The task schema has no environment element, so the pinned environment rides in a companion
    file the launcher applies — named in the action's own arguments, where it cannot be lost."""
    assert WindowsPlatform().environment_file is True
    arguments = find(parse(render()), "Actions/Exec/Arguments").text
    assert "--env-file" in arguments
    assert "SellyAgent.env.json" in arguments


def test_a_path_with_xml_syntax_in_it_is_escaped() -> None:
    tree = parse(render(program_args=[r"C:\py\pythonw.exe", r"C:\a&b\selly"]))
    assert find(tree, "Actions/Exec/Arguments").text == r"C:\a&b\selly"


def test_the_definition_must_be_written_as_utf16() -> None:
    """schtasks refuses an import that is not, and the declaration in the text says so too."""
    assert WindowsPlatform().definition_encoding == "utf-16"
    assert 'encoding="UTF-16"' in render()


# --- schtasks -----------------------------------------------------------------------------------


class RecordingPlatform(WindowsPlatform):
    """A Windows platform whose schtasks calls are recorded rather than run.

    `registered_xml` is what a /Query .. /XML answers — None means no task holds the label."""

    def __init__(self, returncode: int = 0, registered_xml: str | None = None):
        self.calls: list = []
        self._returncode = returncode
        self._existing_xml = registered_xml

    def _schtasks(self, *args):
        import subprocess

        self.calls.append(args)
        if "/Query" in args and "/XML" in args:
            if self._existing_xml is None:
                return subprocess.CompletedProcess(args, 1, "", "the system cannot find the task")
            return subprocess.CompletedProcess(args, 0, self._existing_xml, "")
        return subprocess.CompletedProcess(args, self._returncode, "", "")


def test_registering_imports_the_definition_and_starts_it(tmp_path) -> None:
    """Both, because that is what bootstrapping a plist does: the seller expects the worker to be
    running when setup says it installed one."""
    definition = tmp_path / "SellyAgent.xml"
    definition.write_text("<Task/>", encoding="utf-16")
    platform = RecordingPlatform()

    platform.register(definition)

    assert platform.calls[0] == ("/Query", "/TN", "SellyAgent", "/XML")
    assert platform.calls[1] == ("/Create", "/TN", "SellyAgent", "/XML", str(definition), "/F")
    assert platform.calls[2] == ("/Run", "/TN", "SellyAgent")


def test_registering_replaces_our_own_rather_than_deleting_first(tmp_path) -> None:
    """/F is what keeps a re-install or a mode flip from having a window in which no task exists."""
    definition = tmp_path / "SellyAgent.xml"
    definition.write_text("<Task/>", encoding="utf-16")
    platform = RecordingPlatform(registered_xml=f"<Task>{supervisor.MARKER}</Task>")

    platform.register(definition)

    assert "/Delete" not in [call[0] for call in platform.calls]
    assert ("/Run", "/TN", "SellyAgent") in platform.calls


def test_registering_refuses_to_replace_a_foreign_task(tmp_path) -> None:
    """The registered task is not a file of ours, so the file-based ours-check cannot see it — a
    legacy install's task with the same name would be silently destroyed by /F without this."""
    definition = tmp_path / "SellyAgent.xml"
    definition.write_text("<Task/>", encoding="utf-16")
    platform = RecordingPlatform(registered_xml="<Task>someone else's</Task>")

    with pytest.raises(RegistrationError):
        platform.register(definition)

    assert "/Create" not in [call[0] for call in platform.calls]


def test_a_rejected_definition_is_an_error_not_a_success_message(tmp_path) -> None:
    """schtasks failing to import must not leave install printing 'installed and started'."""
    definition = tmp_path / "SellyAgent.xml"
    definition.write_text("<Task/>", encoding="utf-16")

    with pytest.raises(RegistrationError):
        RecordingPlatform(returncode=1).register(definition)


def test_unregistering_deletes_our_task_without_prompting() -> None:
    platform = RecordingPlatform(registered_xml=f"<Task>{supervisor.MARKER}</Task>")
    platform.unregister("SellyAgent")
    assert platform.calls[-1] == ("/Delete", "/TN", "SellyAgent", "/F")


def test_a_delete_that_fails_is_raised_not_logged() -> None:
    """The keep-alive trigger restarts the daemon within five minutes, so a `daemon stop` that
    reported success over a failed delete would be telling the seller something untrue — and the
    daemon coming back is exactly the symptom nobody would connect to the stop."""
    platform = RecordingPlatform(returncode=1, registered_xml=f"<Task>{supervisor.MARKER}</Task>")
    with pytest.raises(RegistrationError):
        platform.unregister("SellyAgent")


def test_unregistering_leaves_a_foreign_task_alone() -> None:
    platform = RecordingPlatform(registered_xml="<Task>someone else's</Task>")
    platform.unregister("SellyAgent")
    assert "/Delete" not in [call[0] for call in platform.calls]


def test_unregistering_nothing_deletes_nothing() -> None:
    platform = RecordingPlatform()
    platform.unregister("SellyAgent")
    assert "/Delete" not in [call[0] for call in platform.calls]


@pytest.mark.parametrize(("returncode", "expected"), [(0, True), (1, False)])
def test_registration_is_read_from_the_query_result(returncode, expected) -> None:
    assert RecordingPlatform(returncode).is_registered("SellyAgent") is expected


def test_the_task_directory_is_ours_to_clean_up(xdg_tmp) -> None:
    """Windows has no auto-start directory of the OS's own, so the definitions live in our config
    tree — one tree to install, inspect and remove — and an empty tasks dir left behind is our
    litter, unlike ~/Library/LaunchAgents which holds every application's jobs."""
    from selly_agent import paths

    platform = WindowsPlatform()
    assert platform.owns_job_directory is True
    assert platform.launch_agents_dir(Path(r"C:\Users\seller")) is None
    assert paths.launch_agents_dir(platform=platform) == paths.config_dir() / "tasks"
