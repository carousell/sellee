"""Heartbeat file: round-trips {ts, pid}; reads of a missing/garbage file fail soft."""

from __future__ import annotations

import os

from sellee import heartbeat


def test_write_then_read(tmp_path) -> None:
    path = tmp_path / "daemon.heartbeat.json"
    heartbeat.write(path)
    data = heartbeat.read(path)
    assert data["pid"] == os.getpid()
    assert isinstance(data["ts"], float)


def test_read_missing_file_is_none(tmp_path) -> None:
    assert heartbeat.read(tmp_path / "nope.json") is None


def test_read_garbage_is_none(tmp_path) -> None:
    path = tmp_path / "daemon.heartbeat.json"
    path.write_text("not json")
    assert heartbeat.read(path) is None


def test_write_failure_does_not_raise(tmp_path) -> None:
    # a directory path can't be written as a file; write must swallow the error
    heartbeat.write(tmp_path)
