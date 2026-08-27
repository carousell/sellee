"""Runtime code may import the stdlib plus an explicit dependency allowlist, and nothing else.

Dependencies arrive through a uv-managed runtime, which makes adding one easy — so the
guard is what keeps it deliberate. Every runtime dependency is a capability grant on a
machine holding marketplace credentials and a logged-in browser, and it has to appear in
three places to work: pyproject's dependencies, a relocked uv.lock, and ALLOWED_RUNTIME_DEPS
below. An import that skipped that path fails here, in the suite, rather than on a seller's
machine. This walks every import under src/ and flags anything that is neither ours, nor
stdlib, nor allowlisted.

It also enforces that no network module is imported outside a separate explicit allowlist.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
OWN_TOP_LEVEL = "sellee"

# Third-party packages runtime code may import. Keep in sync with [project].dependencies in
# pyproject.toml — this set is the review gate, so a name appears here only alongside a
# relocked uv.lock and a reviewer who accepted the dependency.
# Import names, not distribution names: pillow imports as PIL, pillow-heif as pillow_heif.
ALLOWED_RUNTIME_DEPS = {"psutil", "segno", "PIL", "pillow_heif", "websockets"}

# Network / async modules a runtime module may not import unless its src-relative path is
# listed here. Every entry is a deliberate decision: adding a module here means it is allowed
# to open sockets, and a review should treat that as a real capability grant. `websockets` is a
# dependency rather than stdlib, but it opens sockets like any of the others, so it is gated the
# same way — being on ALLOWED_RUNTIME_DEPS only says the project may use it somewhere, not that
# every module may.
NETWORK_MODULES = {
    "socket",
    "ssl",
    "urllib",
    "http",
    "asyncio",
    "ftplib",
    "smtplib",
    "telnetlib",
    "websockets",
}

# Submodules of a gated top-level that cannot open a socket, named by exact dotted path. These are
# exempt rather than allowlisted per file, because the allowlist below grants socket access and
# these grant none. `urllib.parse` is string manipulation only, and it is what a scheme/host check
# has to use — gating it would push that check toward hand-rolled URL parsing, which is how a host
# check ends up prefix-matching. A bare `import urllib`, or any other urllib submodule, is gated.
NETWORK_SAFE_SUBMODULES = {"urllib.parse"}

NETWORK_ALLOWLIST: set[str] = {
    "sellee/http_server.py",  # the daemon's localhost HTTP server (MCP + tail + control)
    "sellee/mcp_proxy.py",  # stdio shim forwarding JSON-RPC to the daemon over HTTP
    "sellee/control.py",  # the one client for the daemon's localhost control routes
    "sellee/installer/update.py",  # fetches the release archive and its checksums
    "sellee/installer/runtime.py",  # fetches the pinned uv release archive
    "sellee/rail/client.py",  # carousell.ai MCP client + live listing verify
    "sellee/rail/provision.py",  # carousell.ai guest-key provisioning
    "sellee/channel/telegram/transport.py",  # the Telegram Bot API transport (one pipe)
    "sellee/channel/discord/transport.py",  # the Discord REST API transport (one pipe)
    "sellee/channel/discord/ws_client.py",  # the Gateway's WebSocket connection
    "sellee/browser/chrome.py",  # the warm Chrome's CDP readiness probe (loopback GET)
    # Fetches a listing's photographs when the seller asks us to take over what they already had.
    # Narrow by construction: https only, to a host the registry names for that marketplace, no
    # redirects followed, byte-capped per file and per set, and every body sniffed as a real image.
    "sellee/browser/photo_fetch.py",
}


def _stdlib_dirs() -> tuple[str, ...]:
    paths = sysconfig.get_paths()
    return tuple(
        str(Path(paths[key]).resolve()) for key in ("stdlib", "platstdlib") if key in paths
    )


def _site_dirs() -> tuple[str, ...]:
    paths = sysconfig.get_paths()
    return tuple(str(Path(paths[key]).resolve()) for key in ("purelib", "platlib") if key in paths)


def _is_stdlib(name: str) -> bool:
    top = name.split(".")[0]
    if top in sys.builtin_module_names:
        return True
    try:
        spec = importlib.util.find_spec(top)
    except (ImportError, ValueError, ModuleNotFoundError):
        return False
    if spec is None:
        return False
    origin = spec.origin
    if origin in ("built-in", "frozen"):
        return True
    if not origin:
        return False
    resolved = str(Path(origin).resolve())
    if any(resolved.startswith(d) for d in _site_dirs()):
        return False
    return any(resolved.startswith(d) for d in _stdlib_dirs())


def _imported_top_levels(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative (intra-package) import — always internal.
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def _imported_network_modules(tree: ast.AST) -> set[str]:
    """The gated network top-levels a module imports, ignoring socket-free submodules."""
    found: set[str] = set()
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules = [node.module]
        for module in modules:
            if module in NETWORK_SAFE_SUBMODULES:
                continue
            top = module.split(".")[0]
            if top in NETWORK_MODULES:
                found.add(top)
    return found


def _src_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _disallowed_imports(tree: ast.AST, allowed: set) -> set:
    """The imported top-level names a module may not use, given the allowed dependency set."""
    return {
        top
        for top in _imported_top_levels(tree)
        if top != OWN_TOP_LEVEL and top not in allowed and not _is_stdlib(top)
    }


def test_no_non_stdlib_imports_under_src() -> None:
    offenders: list[str] = []
    for path in _src_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for top in sorted(_disallowed_imports(tree, ALLOWED_RUNTIME_DEPS)):
            offenders.append(f"{path.relative_to(ROOT)}: {top}")
    assert not offenders, (
        "imports under src/ that are neither stdlib nor an allowlisted dependency:\n"
        + "\n".join(offenders)
    )


def test_allowlisted_dependency_is_importable() -> None:
    """An allowlisted name must actually resolve — otherwise the allowlist is documentation
    and the runtime it describes was never provisioned."""
    unresolvable = [name for name in ALLOWED_RUNTIME_DEPS if importlib.util.find_spec(name) is None]
    assert not unresolvable, f"allowlisted but not installed: {unresolvable} — run `make bootstrap`"


def test_guard_still_rejects_a_non_allowlisted_dependency() -> None:
    """The allowlist widened the guard by exactly its own contents, and no further."""
    source = "import psutil\nimport requests\nfrom flask import Flask\n"
    tree = ast.parse(source)
    assert _disallowed_imports(tree, ALLOWED_RUNTIME_DEPS) == {"requests", "flask"}
    # With an empty allowlist even psutil is an offender, so the set is doing the work.
    assert "psutil" in _disallowed_imports(tree, set())


def test_no_network_imports_outside_allowlist() -> None:
    offenders: list[str] = []
    for path in _src_files():
        rel = str(path.relative_to(SRC))
        if rel in NETWORK_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for top in sorted(_imported_network_modules(tree)):
            offenders.append(f"{rel}: {top}")
    assert not offenders, "network imports outside the allowlist:\n" + "\n".join(offenders)


def test_the_network_guard_exempts_only_socket_free_submodules() -> None:
    assert _imported_network_modules(ast.parse("from urllib.parse import urlsplit\n")) == set()
    assert _imported_network_modules(ast.parse("import urllib.parse\n")) == set()
    assert _imported_network_modules(ast.parse("import urllib\n")) == {"urllib"}
    assert _imported_network_modules(ast.parse("import urllib.request\n")) == {"urllib"}
    assert _imported_network_modules(ast.parse("from urllib.request import urlopen\n")) == {
        "urllib"
    }
    assert _imported_network_modules(ast.parse("import socket\n")) == {"socket"}
