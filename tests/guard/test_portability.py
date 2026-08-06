"""Guards on the things CI cannot see from one machine.

CI now runs the suite on Linux, macOS and Windows, so "does it work there" is answered by running
it. These cover what a green run on three machines still would not: a module that cannot be
imported at all (invisible until the one code path that imports it runs), a packaged file the code
names but that never shipped, and platform branching leaking out of the modules that own it. All
three were how the previous Windows port rotted — it was never run, so nothing noticed.

What these cannot do is tell you the code *works* on the other platform. Only running it there
can, which is what CI and the live checklist are for.
"""

from __future__ import annotations

import ast
import importlib
import io
import pkgutil
import re
import tokenize
from pathlib import Path

import pytest

import selly_agent

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

# Modules that import a platform's own libraries at module level, and so can only be imported on
# it. Empty on purpose — even platform/windows.py keeps its module-level imports portable — and
# every other module must import anywhere: that is the property under test.
_PLATFORM_ONLY: set = set()


def _module_names() -> list:
    found = []
    for info in pkgutil.walk_packages(selly_agent.__path__, prefix="selly_agent."):
        if info.name not in _PLATFORM_ONLY:
            found.append(info.name)
    return sorted(found)


@pytest.mark.parametrize("name", _module_names())
def test_every_module_imports(name) -> None:
    """An import-time platform assumption — `import fcntl` at the top of a file — takes down the
    daemon before any of its own error handling can say why."""
    importlib.import_module(name)


def test_the_platform_only_list_stays_empty() -> None:
    """It is an exemption from the guard above, so growing it is how the guard stops meaning
    anything. A module that needs one OS's libraries at import time belongs behind the platform
    seam — with the imports inside the functions that use them, as platform/windows.py does."""
    assert _PLATFORM_ONLY == set()


# --- platform branches stay in their owner modules ------------------------------------------

# Where a platform conditional is allowed to live: the platform/ seam (host integration), plus
# the portable modules that each own one per-OS concern and hide it behind a neutral API. A hit
# anywhere else is core logic learning a platform quirk — the scatter that rotted the last port.
#
# Spelled with forward slashes and matched against as_posix(), so the guard means the same thing
# on a host whose separator is a backslash — otherwise every owner silently stops being one.
_PLATFORM_OWNERS = {
    # The module that defines the question. Every other owner asks it through here.
    "selly_agent/host.py",
    "selly_agent/platform/__init__.py",
    "selly_agent/platform/base.py",
    "selly_agent/platform/macos.py",
    "selly_agent/platform/windows.py",
    "selly_agent/paths.py",
    "selly_agent/lock.py",
    "selly_agent/pointer.py",
    "selly_agent/spawn.py",
    "selly_agent/proc_tree.py",
    "selly_agent/supervisor.py",
    "selly_agent/http_server.py",
    "selly_agent/setup_cli.py",
    "selly_agent/uninstall_cli.py",
    "selly_agent/browser/chrome.py",
    "selly_agent/installer/preflight.py",
    "selly_agent/installer/materialize.py",
    "selly_agent/installer/runtime.py",
    "selly_agent/installer/ui.py",
}

_PLATFORM_TOKENS = re.compile(
    r"\bos\.name\b|\bsys\.platform\b|\bimport platform\b|"
    r"\bmsvcrt\b|\bwinreg\b|\b_winapi\b|\bfcntl\b|\bctypes\b|"
    # host.py's own helpers count as platform tokens. Without this line the tidier spelling
    # would be an unpoliced way to branch on the OS anywhere in the tree — the abstraction
    # would have quietly removed the rule it was supposed to make easier to follow.
    r"\bhost\.windows\b|\bhost\.macos\b|\bhost\.linux\b|\bhost\.name\b"
)


def _without_comments(source: str) -> str:
    """Comments blanked: prose about a platform is not a platform branch. Strings are still
    scanned — an os.environ key is a string."""
    lines = source.splitlines()
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                row = token.start[0] - 1
                lines[row] = lines[row].replace(token.string, "")
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source
    return "\n".join(lines)


def test_platform_conditionals_live_in_their_owner_modules() -> None:
    """The two-place rule from AGENTS.md, made mechanical: a daemon that needs a path, a lock, a
    spawn or a kill calls a platform-neutral function; only the module that owns the concern may
    ask what OS this is. Growing the owner list is a deliberate, visible act."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in _PLATFORM_OWNERS:
            continue
        for lineno, line in enumerate(
            _without_comments(path.read_text(encoding="utf-8")).splitlines(), start=1
        ):
            if _PLATFORM_TOKENS.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "platform conditionals outside their owner modules:\n" + "\n".join(
        offenders
    )


# The one module allowed to ask the OS directly; everything else goes through its helpers.
_HOST_MODULE = "selly_agent/host.py"

_RAW_HOST_QUESTION = re.compile(r"os\.name\s*[=!]=|sys\.platform\s*[=!]=|sys\.platform\.startswith")


def test_the_host_question_is_asked_one_way() -> None:
    """`os.name == "nt"`, `os.name != "nt"` and `sys.platform == "win32"` all answer the same
    question, and the tree used all three — twice in one module. They agree, which is exactly why
    the inconsistency survived: nothing was ever wrong, so nothing forced a decision.

    host.py is the one place that asks. This keeps the older spellings from drifting back, which
    a rename alone would not: nothing else would have noticed the next `os.name == "nt"`.

    Not covered here: bin/selly-agent, which has no .py extension and so is not walked anyway.
    The launcher runs before the venv exists and cannot import host, so its checks stay inline —
    the one place two spellings is the lesser evil, and it is called out in host.py's docstring.
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == _HOST_MODULE:
            continue
        for lineno, line in enumerate(
            _without_comments(path.read_text(encoding="utf-8")).splitlines(), start=1
        ):
            if _RAW_HOST_QUESTION.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "the host is asked directly outside host.py (use host.windows()/macos()/linux()):\n"
        + "\n".join(offenders)
    )


# --- files the code names -----------------------------------------------------------------------

# The roots a shipped asset is addressed from. A name joined onto one of these is a file that has
# to be in the package, and a missing one is not discovered until the code path that reads it runs.
_ASSET_ROOTS = ("PACKAGE_DATA_DIR", "SKILLS_DIR")


def _named_assets():
    """Every `<asset root> / "name"` in the source, as (source file, resolved path)."""
    from selly_agent.paths import PACKAGE_DATA_DIR
    from selly_agent.skills import SKILLS_DIR

    roots = {"PACKAGE_DATA_DIR": PACKAGE_DATA_DIR, "SKILLS_DIR": SKILLS_DIR}
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
                continue
            if not (isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
                continue
            left = node.left
            name = left.attr if isinstance(left, ast.Attribute) else getattr(left, "id", "")
            if name in _ASSET_ROOTS:
                yield path, roots[name] / node.right.value


def test_every_packaged_asset_the_code_names_is_present() -> None:
    """A renamed or forgotten asset surfaces when the code path that reads it runs — the web tail
    on a page load, the uv pin at install time — which is a long way from where the name was
    typed."""
    named = list(_named_assets())
    assert named, "the walk found no packaged assets, so it is not checking anything"

    missing = [
        f"{source.relative_to(ROOT).as_posix()}: {asset.name}"
        for source, asset in named
        if not asset.exists()
    ]
    assert not missing, "assets named in the code but not in the package:\n" + "\n".join(missing)


def test_the_files_a_version_is_built_from_are_all_here() -> None:
    """The staging list is the authority on what a release contains, so a name in it that does not
    exist yields a version that installs and then cannot run."""
    from selly_agent.installer import materialize

    for name in materialize.VERSION_DIRS:
        assert (ROOT / name).is_dir(), name
    for name in materialize.VERSION_FILES:
        if name == "LICENSE":
            continue  # copied conditionally; absent from the repo today
        assert (ROOT / name).is_file(), name
