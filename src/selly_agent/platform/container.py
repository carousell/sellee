"""The container platform — image conversion, and nothing else.

Half of the `Platform` surface describes how an OS keeps a background job alive. In a container
there is no such job: Docker runs this process, restarts it by its own policy, and stops it on
`docker compose stop`. A supervisor implementation here would render a job definition describing
something that does not exist, so every one of those methods refuses instead — and the verbs that
would reach them (`daemon install|start|stop`, `update`, `uninstall`) refuse earlier, in their own
words, naming the Docker command that does the job.

What is left is the one genuinely OS-shaped thing the daemon needs: turning a photo the
marketplaces will not take into one they will. macOS has `sips`; the image ships ImageMagick.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from selly_agent.platform.base import ImageToolUnavailable, Platform, UnsupportedPlatform

_CONVERT_TIMEOUT_SEC = 30.0

# ImageMagick 7's binary, which is the one the image installs. Its 6.x predecessor called this
# `convert`; accepting both would mean carrying a fallback for an image we build ourselves.
_IMAGE_TOOL = "magick"


class ContainerPlatform(Platform):
    name = "container"

    # --- images ------------------------------------------------------------------------------

    def to_jpeg(self, src: Path, dest: Path, max_dim: int) -> None:
        tool = shutil.which(_IMAGE_TOOL)
        if not tool:
            raise ImageToolUnavailable(
                f"cannot convert {src.name}: `{_IMAGE_TOOL}` is missing from this image"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        argv = [
            tool,
            # `[0]` takes the first frame only: a phone's HEIC can carry a depth map or a burst
            # alongside the picture, and left to itself ImageMagick writes one numbered file per
            # frame — none of them at the path we promised the caller.
            f"{src}[0]",
            "-auto-orient",
            # The trailing `>` is ImageMagick's "only shrink" flag: a photo already under the
            # limit is re-encoded, never enlarged.
            "-resize",
            f"{max_dim}x{max_dim}>",
            f"jpg:{dest}",
        ]
        try:
            proc = subprocess.run(  # noqa: S603 — argv is composed here, not a shell string
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=_CONVERT_TIMEOUT_SEC,
            )
        except FileNotFoundError as exc:
            raise ImageToolUnavailable(
                f"cannot convert {src.name}: `{_IMAGE_TOOL}` is missing from this image"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ImageToolUnavailable(f"converting {src.name} timed out") from exc
        if proc.returncode != 0 or not dest.exists():
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            reason = detail[-1] if detail else f"{_IMAGE_TOOL} exited {proc.returncode}"
            raise ImageToolUnavailable(f"cannot convert {src.name}: {reason}")

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
