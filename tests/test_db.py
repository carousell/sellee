"""Read-only connections: a portable URI, no writer required, and writes actually refused."""

from __future__ import annotations

import sqlite3

import pytest

from selly_agent import db


def test_read_only_uri_is_a_url_rather_than_an_interpolated_path(tmp_path) -> None:
    uri = db.read_only_uri(tmp_path / "a b" / "selly.db")
    assert uri.startswith("file:///")
    assert uri.endswith("?mode=ro")
    # Escaped, so a path's own characters can never be read as URI syntax.
    assert " " not in uri


def test_reader_opens_a_path_needing_escaping(tmp_path) -> None:
    path = tmp_path / "with space" / "selly.db"
    path.parent.mkdir()
    writer = db.connect_writer(path)
    writer.execute("CREATE TABLE t (v TEXT)")
    writer.execute("INSERT INTO t VALUES ('x')")
    writer.close()

    reader = db.connect_reader(path)
    try:
        assert [row[0] for row in reader.execute("SELECT v FROM t")] == ["x"]
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO t VALUES ('y')")
    finally:
        reader.close()
