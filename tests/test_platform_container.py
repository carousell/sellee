"""The container platform: it converts images, and it refuses to describe a supervisor job.

The image conversion is exercised against a stand-in binary on PATH rather than a real
ImageMagick, so what is actually pinned is the argv — first frame only, shrink-only resize, an
explicit JPEG output — and the failure shape, which is what the photo pipeline degrades on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from selly_agent.platform import get_platform
from selly_agent.platform.base import ImageToolUnavailable, Platform, UnsupportedPlatform
from selly_agent.platform.container import ContainerPlatform

# Records its own argv, then writes the file it was asked for so the caller sees a conversion.
_FAKE_TOOL = """#!/bin/sh
last=""
for arg in "$@"; do
	printf '%s\\n' "$arg" >> "$ARGV_LOG"
	last="$arg"
done
printf 'JPEG' > "${last#jpg:}"
"""

_FAILING_TOOL = """#!/bin/sh
echo "no decode delegate for HEIC" >&2
exit 1
"""


@pytest.fixture
def fake_magick(tmp_path, monkeypatch):
    """A `magick` on PATH that records its arguments and produces its output file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "argv.log"
    tool = bin_dir / "magick"
    tool.write_text(_FAKE_TOOL)
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("ARGV_LOG", str(log))
    return log


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "a.heic"
    src.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"heicbytes")
    return src


# --- selection ------------------------------------------------------------------------------


def test_the_marker_selects_the_container_platform(container) -> None:
    assert isinstance(get_platform(), ContainerPlatform)


def test_the_marker_wins_over_the_host_os(container, monkeypatch) -> None:
    """The image decides what this runs on, not the kernel underneath it — so a Docker Desktop
    Mac, where the daemon still runs in a Linux container, must not resolve to macOS."""
    monkeypatch.setattr("sys.platform", "darwin")
    assert isinstance(get_platform(), ContainerPlatform)


def test_without_the_marker_linux_is_still_unsupported(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    with pytest.raises(UnsupportedPlatform):
        get_platform()


# --- images ---------------------------------------------------------------------------------


def test_a_photo_is_converted_through_the_image_tool(tmp_path, fake_magick) -> None:
    dest = tmp_path / "out" / "a.jpg"
    ContainerPlatform().to_jpeg(_source(tmp_path), dest, 1600)
    assert dest.read_bytes() == b"JPEG"


def test_the_conversion_takes_one_frame_and_only_ever_shrinks(tmp_path, fake_magick) -> None:
    """A phone's HEIC can carry a depth map beside the picture, and a photo already under the
    limit must not be blown up to meet it."""
    ContainerPlatform().to_jpeg(_source(tmp_path), tmp_path / "a.jpg", 1600)
    argv = fake_magick.read_text().splitlines()
    assert argv[0].endswith("a.heic[0]")
    assert "1600x1600>" in argv
    assert argv[-1] == f"jpg:{tmp_path / 'a.jpg'}"


def test_a_missing_image_tool_names_the_file_it_could_not_convert(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(ImageToolUnavailable, match="a.heic"):
        ContainerPlatform().to_jpeg(_source(tmp_path), tmp_path / "a.jpg", 1600)


def test_a_failing_conversion_carries_the_tools_own_reason(tmp_path, monkeypatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "magick"
    tool.write_text(_FAILING_TOOL)
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    with pytest.raises(ImageToolUnavailable, match="no decode delegate"):
        ContainerPlatform().to_jpeg(_source(tmp_path), tmp_path / "a.jpg", 1600)


# --- the supervisor half ----------------------------------------------------------------------


def test_every_supervisor_question_is_refused_rather_than_answered(tmp_path) -> None:
    """Docker supervises this process. A rendered job definition would describe something that
    does not exist, and a plausible-looking answer is worse than a refusal."""
    platform = ContainerPlatform()
    calls = (
        lambda: platform.launch_agents_dir(tmp_path),
        lambda: platform.default_label(),
        lambda: platform.supervisor_filename("com.selly.agent"),
        lambda: platform.register(tmp_path / "job"),
        lambda: platform.unregister("com.selly.agent"),
        lambda: platform.is_registered("com.selly.agent"),
        lambda: platform.render_supervisor(
            label="com.selly.agent",
            program_args=["/bin/true"],
            stdout_path=tmp_path / "out",
            stderr_path=tmp_path / "err",
            marker="m",
            environment={},
        ),
    )
    for call in calls:
        with pytest.raises(UnsupportedPlatform, match="container runtime"):
            call()


def test_the_container_platform_implements_the_whole_interface() -> None:
    """Instantiable at all means no abstract method was left out — a missing one would only
    surface as a TypeError the first time a container resolved its platform."""
    assert isinstance(ContainerPlatform(), Platform)
