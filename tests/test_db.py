"""Read-only connections address the file they were given, whatever its path contains."""

from __future__ import annotations

import sqlite3

import pytest

from sellee import db


@pytest.mark.parametrize("directory", ["with space", "what?", "hash#tag", "pct%20dir"])
def test_a_reader_opens_the_database_its_path_names(tmp_path, directory) -> None:
    """Pasted into a `file:` URI as-is, `?` and `#` end the path and `%20` decodes to a space, so
    the reader opens some other database, or none."""
    path = tmp_path / directory / "sellee.db"
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
