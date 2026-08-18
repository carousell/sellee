"""The launcher's re-exec decision.

The launcher runs before the venv exists, so it cannot import anything of ours. Its one piece
of logic is "am I on the right interpreter", and getting that wrong in either direction is bad:
never re-exec and the dependencies are missing, re-exec unconditionally and it loops.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "bin" / "sellee"


@pytest.fixture(scope="module")
def launcher():
    """Load bin/sellee as a module without running its dispatch.

    Importing it would re-exec and then call main(); loading the source and executing only the
    function definitions is enough to test the decision.
    """
    source = LAUNCHER.read_text()
    # Everything up to the module-level work is the part under test.
    head = source.split("_here = os.path.dirname")[0]
    namespace = {"__name__": "launcher_under_test", "os": os}
    exec(compile(head, str(LAUNCHER), "exec"), namespace)
    return namespace


def _make_venv(tree: Path) -> Path:
    interpreter = tree / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n")
    interpreter.chmod(0o755)
    return interpreter


def test_no_venv_means_carry_on(launcher, tmp_path):
    """A checkout nobody has bootstrapped still runs dependency-free commands."""
    assert launcher["reexec_target"](str(tmp_path), prefix="/usr") is None


def test_a_venv_we_are_not_in_is_the_target(launcher, tmp_path):
    interpreter = _make_venv(tmp_path)
    assert launcher["reexec_target"](str(tmp_path), prefix="/usr") == str(interpreter)


def test_already_inside_the_venv_does_not_re_exec(launcher, tmp_path):
    """This is the exec-loop guard: after re-exec, sys.prefix is the venv."""
    _make_venv(tmp_path)
    assert launcher["reexec_target"](str(tmp_path), prefix=str(tmp_path / ".venv")) is None


def test_the_store_interpreter_outside_the_venv_still_re_execs(launcher, tmp_path):
    """A venv's python symlinks into uv's shared store. Running that store interpreter directly
    gets none of the venv's packages, so it must still be corrected — the reason membership is
    decided by prefix rather than by comparing resolved interpreter paths.
    """
    store = tmp_path / "uv-store" / "cpython-3.14" / "bin"
    store.mkdir(parents=True)
    real = store / "python3.14"
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)

    tree = tmp_path / "tree"
    (tree / ".venv" / "bin").mkdir(parents=True)
    link = tree / ".venv" / "bin" / "python"
    link.symlink_to(real)

    # sys.prefix when running the store interpreter directly is the store, not the venv.
    target = launcher["reexec_target"](
        str(tree), prefix=str(tmp_path / "uv-store" / "cpython-3.14")
    )
    assert target == str(link)


def test_a_symlinked_venv_prefix_is_recognised_as_the_same_venv(launcher, tmp_path):
    """current -> versions/<v> is a symlink, so the tree reached through it and the tree itself
    must count as one venv; otherwise every launch through `current` would re-exec."""
    real_tree = tmp_path / "versions" / "1.0.0"
    real_tree.mkdir(parents=True)
    _make_venv(real_tree)
    link = tmp_path / "current"
    link.symlink_to(real_tree)
    assert launcher["reexec_target"](str(link), prefix=str(real_tree / ".venv")) is None


def test_venv_interpreter_follows_virtualenv_layout(launcher, tmp_path):
    resolved = launcher["venv_interpreter"](str(tmp_path))
    expected = "Scripts" if os.name == "nt" else "bin"
    assert Path(resolved).parent.name == expected
    assert Path(resolved).parent.parent.name == ".venv"


def test_launcher_imports_nothing_beyond_the_stdlib():
    """It runs before the venv, so a third-party import here could never resolve — and an
    install that cannot start is an install that cannot say why."""
    tree = ast.parse(LAUNCHER.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported == {"__future__", "os", "sys", "sellee"}
