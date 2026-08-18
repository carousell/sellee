"""The container platform: it refuses to describe a supervisor job, and it is selected by the
image's own marker rather than by the kernel underneath it.

Photo conversion is not tested here — the container does not implement it. It is a bundled
dependency behaving the same everywhere, covered in test_platform_images.py.
"""

from __future__ import annotations

import pytest

from sellee.platform import get_platform
from sellee.platform.base import Platform, UnsupportedPlatform
from sellee.platform.container import ContainerPlatform

# --- selection ------------------------------------------------------------------------------


def test_the_marker_selects_the_container_platform(container) -> None:
    assert isinstance(get_platform(), ContainerPlatform)


@pytest.mark.parametrize("host", ["darwin", "linux"])
def test_the_marker_wins_over_the_host_os(container, monkeypatch, host) -> None:
    """The image decides what this runs on, not the kernel underneath it. A Docker Desktop Mac
    must not resolve to macOS — and the image's own kernel *is* Linux, so a container on a Linux
    host must get these refusals rather than a systemd user unit it has no manager for."""
    monkeypatch.setattr("sys.platform", host)
    assert isinstance(get_platform(), ContainerPlatform)


def test_an_unsupported_host_os_is_refused(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "freebsd14")
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
        lambda: platform.supervisor_filename("com.sellee.agent"),
        lambda: platform.register(tmp_path / "job"),
        lambda: platform.unregister("com.sellee.agent"),
        lambda: platform.is_registered("com.sellee.agent"),
        lambda: platform.render_supervisor(
            label="com.sellee.agent",
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
