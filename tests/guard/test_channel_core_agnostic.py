"""The channel core must stay provider-agnostic: no core module may import a provider package.

This is the structural guarantee behind "adding a channel is a new provider package, not changes
to the core" — if a core module reaches into `channel.telegram`, the seam has leaked and this test
fails, naming the offender. Providers depending on the core is fine (the arrow points one way).
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
_CORE = SRC / "sellee" / "channel"
# The provider-agnostic core modules — everything directly under channel/, excluding provider
# subpackages (channel/telegram/ and future siblings).
_CORE_MODULES = [p for p in _CORE.glob("*.py")]


def _imports(path: Path) -> set:
    names: set = set()
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    # resolve `from . / from .. import x` relative imports to their leaf too
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            for alias in node.names:
                names.add(alias.name)
    return names


def test_core_modules_do_not_import_a_provider() -> None:
    offenders: list = []
    for path in _CORE_MODULES:
        for name in _imports(path):
            # a provider is a subpackage of channel; the core names it via `.telegram`,
            # `telegram`, or a fully-qualified `sellee.channel.telegram`.
            leaf = name.split(".")[-1]
            if "telegram" in name.split(".") or leaf == "telegram":
                offenders.append(f"{path.name}: imports {name}")
    assert not offenders, "channel core leaked into a provider:\n" + "\n".join(offenders)


def test_core_has_the_expected_modules() -> None:
    # guard against the scan silently covering nothing if files move
    got = {p.name for p in _CORE_MODULES}
    assert {"fastpaths.py", "routing.py", "outbound.py", "prompt.py"} <= got
