"""The container platform — refusals, and the inherited converter.

The `Platform` surface describes how an OS keeps a background job alive. In a container there is
no such job: Docker runs this process, restarts it by its own policy, and stops it on
`docker compose stop`. A supervisor implementation here would render a job definition describing
something that does not exist, so every one of those methods refuses instead — and the verbs that
would reach them (`daemon install|start|stop`, `update`, `uninstall`) refuse earlier, in their own
words, naming the Docker command that does the job.

Photo conversion is inherited rather than overridden: the converter behaves the same here.
"""

from __future__ import annotations

from pathlib import Path

from selly_agent.platform.base import Platform, UnsupportedPlatform


class ContainerPlatform(Platform):
    name = "container"

    # --- supervisor: not ours here -------------------------------------------------------------

    def _refuse(self, what: str):
        return UnsupportedPlatform(
            f"{what} has no meaning in a container: this process belongs to the container, and "
            "keeping it alive is the container runtime's job."
        )

    def launch_agents_dir(self, home: Path) -> Path:
        raise self._refuse("a per-user auto-start directory")

    def default_label(self) -> str:
        raise self._refuse("a supervisor job label")

    def supervisor_filename(self, label: str) -> str:
        raise self._refuse("a supervisor job file")

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
        raise self._refuse("a supervisor job definition")

    def register(self, config_path: Path) -> None:
        raise self._refuse("registering a supervisor job")

    def unregister(self, label: str) -> None:
        raise self._refuse("unregistering a supervisor job")

    def is_registered(self, label: str) -> bool:
        raise self._refuse("asking a supervisor about a job")
