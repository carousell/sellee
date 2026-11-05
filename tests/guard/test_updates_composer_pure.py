"""The activity-update composer stays pure, the way an engine does.

It turns ledger rows into the words the seller reads, so what it may reach is what keeps an
update honest: no store (it cannot read the floor, or anything else, behind the caller's back), no
tool or server module, no network, no clock (the caller hands it `now`, so the same rows always
compose the same message), and no file, process or randomness. Only the pure engines and a short
list of stdlib modules that compute on what they are handed are within reach; adding one to that
list is a deliberate act.
"""

from __future__ import annotations

import ast
from pathlib import Path

COMPOSER = Path(__file__).resolve().parents[2] / "src" / "sellee" / "updates.py"

# The one part of the tree the composer may lean on: pure decision layers, network-free by their
# own guard.
_ALLOWED_SELLEE_PACKAGE = "sellee.engines"
# The stdlib it may use, by top-level module: each only computes on the values it is given.
_ALLOWED_STDLIB = {
    "__future__",
    "collections",
    "dataclasses",
    "math",
    "re",
    "typing",
    "unicodedata",
}


def _imported_modules(tree: ast.AST) -> set:
    names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def _is_engine(name: str) -> bool:
    return name == _ALLOWED_SELLEE_PACKAGE or name.startswith(_ALLOWED_SELLEE_PACKAGE + ".")


def _offenders(tree: ast.AST) -> list:
    offenders = []
    for name in sorted(_imported_modules(tree)):
        top = name.split(".")[0]
        if top == "sellee":
            if name != "sellee" and not _is_engine(name):
                offenders.append(name)
        elif top not in _ALLOWED_STDLIB:
            offenders.append(name)
    return offenders


def test_the_composer_imports_only_engines_and_pure_stdlib() -> None:
    tree = ast.parse(COMPOSER.read_text(), filename=str(COMPOSER))
    offenders = _offenders(tree)
    assert offenders == [], (
        "sellee/updates.py must stay pure — the caller reads the store and the clock and hands "
        "the composer what it needs:\n" + "\n".join(offenders)
    )


def test_the_scan_catches_what_it_is_meant_to() -> None:
    source = (
        "from sellee.store import Store\n"
        "from sellee.tools.registry import dispatch\n"
        "import sellee.http_server\n"
        "from sellee.channel import outbound\n"
        "import urllib.request\n"
        "import time\n"
        "from datetime import datetime\n"
        "import sqlite3\n"
        "import os\n"
        "import random\n"
        "import subprocess\n"
        "from sellee.engines_helpers import peek\n"
        "from sellee.engines import hosts\n"
        "from sellee.engines.hosts import extract_links\n"
        "import re\n"
        "from collections.abc import Sequence\n"
    )
    assert _offenders(ast.parse(source)) == [
        "datetime",
        "datetime.datetime",
        "os",
        "random",
        "sellee.channel",
        "sellee.channel.outbound",
        "sellee.engines_helpers",
        "sellee.engines_helpers.peek",
        "sellee.http_server",
        "sellee.store",
        "sellee.store.Store",
        "sellee.tools.registry",
        "sellee.tools.registry.dispatch",
        "sqlite3",
        "subprocess",
        "time",
        "urllib.request",
    ]
