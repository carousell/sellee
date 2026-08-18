"""The runtime layer against a real uv: lock replay, and a compiled dependency that imports.

Skipped when no usable uv is present, because it downloads an interpreter and a wheel set.
This is the test that would have caught a lock the target platform has no wheel for — the
reason the dependency posture is worth having at all is that a seller's machine has no
compiler, so "it resolves" and "it installs" have to be the same statement.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from sellee import paths
from sellee.installer import runtime

ROOT = Path(__file__).resolve().parents[2]


def _available_uv():
    found = shutil.which("uv")
    if found and runtime.serves_pin(Path(found), runtime.read_python_pin(ROOT)):
        return Path(found)
    return None


uv_binary = _available_uv()

pytestmark = pytest.mark.skipif(
    uv_binary is None,
    reason="needs a uv on PATH that can serve the pinned interpreter (run `make bootstrap`)",
)


@pytest.fixture(scope="module")
def provisioned(tmp_path_factory):
    """A staged tree with just the files a sync needs, provisioned like a version directory."""
    tree = tmp_path_factory.mktemp("staged")
    for name in ("pyproject.toml", "uv.lock", ".python-version"):
        shutil.copy2(ROOT / name, tree / name)
    interpreter = runtime.sync(uv_binary, tree)
    return tree, interpreter


def test_sync_builds_a_venv_at_the_path_the_rest_of_the_code_expects(provisioned):
    tree, interpreter = provisioned
    assert interpreter == paths.venv_python(tree)
    assert interpreter.exists()


def test_the_venv_interpreter_is_the_pinned_final_release(provisioned):
    _, interpreter = provisioned
    pinned = runtime.read_python_pin(ROOT)
    probe = "import sys; print('%d.%d' % sys.version_info[:2], sys.version_info.releaselevel)"
    out = subprocess.run(
        [str(interpreter), "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert out[0] == pinned
    assert out[1] == "final", "a pre-release interpreter must never be what we provision"


def test_the_compiled_dependency_imports_and_works(provisioned):
    """psutil is a C extension: it having installed from a wheel and answering a real call is
    the whole claim being made about taking dependencies this way."""
    _, interpreter = provisioned
    result = subprocess.run(
        [str(interpreter), "-c", "import os, psutil; print(psutil.pid_exists(os.getpid()))"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "True"


def test_our_own_package_is_not_installed_into_the_venv(provisioned):
    """The venv carries dependencies only. An editable install would build this package on a
    user's machine and leave a path pinned into a directory that update prunes."""
    _, interpreter = provisioned
    result = subprocess.run(
        [str(interpreter), "-c", "import sellee"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "ModuleNotFoundError" in result.stderr


def test_sync_is_offline_once_the_cache_is_warm(provisioned):
    """A second sync of the same lock must not need the network — this is what makes an update
    on a flaky connection recoverable rather than half-done."""
    tree, _ = provisioned
    result = subprocess.run(
        [str(uv_binary), "sync", "--locked", "--no-dev", "--offline"],
        cwd=str(tree),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
