"""Spawn-time program resolution: found programs become full paths, missing ones are left alone."""

from __future__ import annotations

import os
import stat

from selly_agent import spawn


def _executable(path, body: str = "#!/bin/sh\nexit 0\n"):
    """A program the host will actually resolve. On Windows that means one of PATHEXT's
    extensions — an extensionless file is not executable there, so `which` walks past it."""
    if os.name == "nt":
        path = path.with_suffix(".cmd")
        body = "@echo off\r\nexit /b 0\r\n"
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_a_program_on_path_is_resolved_to_its_full_path(tmp_path, monkeypatch) -> None:
    binary = _executable(tmp_path / "npx")
    monkeypatch.setenv("PATH", str(tmp_path))
    resolved = spawn.resolve(["npx", "--yes", "pkg"])
    # normcase: Windows hands back the extension in PATHEXT's own casing (npx.CMD), and its
    # paths are case-insensitive, so comparing the raw strings would fail on spelling alone.
    assert os.path.normcase(resolved[0]) == os.path.normcase(str(binary))
    assert resolved[1:] == ["--yes", "pkg"]


def test_a_missing_program_is_left_for_the_caller_to_report(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    assert spawn.resolve(["npx", "--yes"]) == ["npx", "--yes"]


def test_arguments_are_stringified_but_never_resolved(tmp_path, monkeypatch) -> None:
    _executable(tmp_path / "npx")
    monkeypatch.setenv("PATH", str(tmp_path))
    resolved = spawn.resolve(["npx", tmp_path / "npx"])
    assert resolved[1] == str(tmp_path / "npx")


def test_an_empty_argv_is_returned_unchanged() -> None:
    assert spawn.resolve([]) == []


def test_an_absolute_program_survives_resolution(tmp_path, monkeypatch) -> None:
    """The recorded values setup writes are already absolute, and must not need PATH to work."""
    binary = _executable(tmp_path / "claude")
    monkeypatch.setenv("PATH", "")
    assert spawn.resolve([str(binary), "-p"]) == [str(binary), "-p"]
