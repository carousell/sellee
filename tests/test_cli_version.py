"""The version subcommand prints the single __version__ constant."""

from __future__ import annotations

from sellee import __version__
from sellee.cli import main


def test_version_prints_constant(capsys) -> None:
    rc = main(["sellee", "version"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == __version__
