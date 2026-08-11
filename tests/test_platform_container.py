"""The container platform: it refuses to describe a supervisor job, and it is selected by the
image's own marker rather than by the kernel underneath it.

Photo conversion is not tested here — the container does not implement it. It is a bundled
dependency behaving the same everywhere, covered in test_platform_images.py.
"""

from __future__ import annotations

import pytest

from selly_agent.platform import get_platform
from selly_agent.platform.base import Platform, UnsupportedPlatform
from selly_agent.platform.container import ContainerPlatform

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
