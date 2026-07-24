"""Runtime code must be stdlib-only, and plan-02 code must do no network I/O.

The install story depends on the user's own python3 being the only runtime dependency,
so a stray pip import has to fail here — in the suite — not on a tester's machine. This
walks every import under src/ and fails on any module that is neither our own package nor
part of the standard library. It also enforces that no network module is imported outside
an explicit allowlist (empty today; later workstreams that add the update check / channel
poller extend it).
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
OWN_TOP_LEVEL = "selly_agent"

# Network / async modules a runtime module may not import unless its src-relative path is
# listed here. Every entry is a deliberate decision: adding a module here means it is allowed
# to open sockets, and a review should treat that as a real capability grant.
NETWORK_MODULES = {"socket", "ssl", "urllib", "http", "asyncio", "ftplib", "smtplib", "telnetlib"}
NETWORK_ALLOWLIST: set[str] = {
    "selly_agent/http_server.py",  # the daemon's localhost HTTP server (MCP + tail + control)
    "selly_agent/mcp_proxy.py",  # stdio shim forwarding JSON-RPC to the daemon over HTTP
    "selly_agent/pass_cli.py",  # `pass run` posts to the daemon's control route
    "selly_agent/connect_cli.py",  # `connect telegram` posts the token to the control route
    "selly_agent/settings_cli.py",  # `settings list|approve|cancel|undo` over the control route
    "selly_agent/rail/client.py",  # carousell.ai MCP client + live listing verify
    "selly_agent/rail/provision.py",  # carousell.ai guest-key provisioning
    "selly_agent/channel/telegram/transport.py",  # the Telegram Bot API transport (one pipe)
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


def _src_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_no_non_stdlib_imports_under_src() -> None:
    offenders: list[str] = []
    for path in _src_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for top in _imported_top_levels(tree):
            if top == OWN_TOP_LEVEL or _is_stdlib(top):
                continue
            offenders.append(f"{path.relative_to(ROOT)}: {top}")
    assert not offenders, "non-stdlib imports under src/:\n" + "\n".join(offenders)


def test_no_network_imports_outside_allowlist() -> None:
    offenders: list[str] = []
    for path in _src_files():
        rel = str(path.relative_to(SRC))
        if rel in NETWORK_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for top in _imported_top_levels(tree):
            if top in NETWORK_MODULES:
                offenders.append(f"{rel}: {top}")
    assert not offenders, "network imports outside the allowlist:\n" + "\n".join(offenders)
