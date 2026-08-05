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
from selly_agent.platform.windows import _TASK_NAMESPACE, WindowsPlatform

GOLDEN = Path(__file__).resolve().parent / "golden" / "SellyAgent.xml"

PROGRAM_ARGS = [
    r"C:\Users\seller\AppData\Local\selly-agent\share\current\.venv\Scripts\pythonw.exe",
    r"C:\Users\seller\AppData\Local\selly-agent\share\current\bin\selly-agent",
    "daemon",
    "run",
]


def render(**overrides) -> str:
    defaults = {
        "label": "SellyAgent",
        "program_args": PROGRAM_ARGS,
        "stdout_path": Path(r"C:\logs\agent.out.log"),
        "stderr_path": Path(r"C:\logs\agent.err.log"),
        "marker": supervisor.MARKER,
        "environment": {},
        "start_at_login": True,
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


def test_a_path_with_xml_syntax_in_it_is_escaped() -> None:
    tree = parse(render(program_args=[r"C:\py\pythonw.exe", r"C:\a&b\selly"]))
    assert find(tree, "Actions/Exec/Arguments").text == r"C:\a&b\selly"


def test_the_definition_must_be_written_as_utf16() -> None:
    """schtasks refuses an import that is not, and the declaration in the text says so too."""
    assert WindowsPlatform().definition_encoding == "utf-16"
    assert 'encoding="UTF-16"' in render()


# --- schtasks -----------------------------------------------------------------------------------


class RecordingPlatform(WindowsPlatform):
    """A Windows platform whose schtasks calls are recorded rather than run."""

    def __init__(self, returncode: int = 0):
        self.calls: list = []
        self._returncode = returncode

    def _schtasks(self, *args):
        import subprocess

        self.calls.append(args)
        return subprocess.CompletedProcess(args, self._returncode, "", "")


def test_registering_imports_the_definition_and_starts_it(tmp_path) -> None:
    """Both, because that is what bootstrapping a plist does: the seller expects the worker to be
    running when setup says it installed one."""
    definition = tmp_path / "SellyAgent.xml"
    definition.write_text("<Task/>", encoding="utf-16")
    platform = RecordingPlatform()

    platform.register(definition)

    assert platform.calls[0] == ("/Create", "/TN", "SellyAgent", "/XML", str(definition), "/F")
    assert platform.calls[1] == ("/Run", "/TN", "SellyAgent")


def test_registering_replaces_rather_than_deleting_first(tmp_path) -> None:
    """/F is what keeps a re-install or a mode flip from having a window in which no task exists."""
    definition = tmp_path / "SellyAgent.xml"
    definition.write_text("<Task/>", encoding="utf-16")
    platform = RecordingPlatform()

    platform.register(definition)

    assert "/Delete" not in [call[0] for call in platform.calls]


def test_unregistering_does_not_prompt() -> None:
    platform = RecordingPlatform()
    platform.unregister("SellyAgent")
    assert platform.calls == [("/Delete", "/TN", "SellyAgent", "/F")]


@pytest.mark.parametrize(("returncode", "expected"), [(0, True), (1, False)])
def test_registration_is_read_from_the_query_result(returncode, expected) -> None:
    assert RecordingPlatform(returncode).is_registered("SellyAgent") is expected
