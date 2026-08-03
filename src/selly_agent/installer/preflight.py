"""The machine gates setup runs before it writes anything.

Each gate is split in two: a pure function that decides an answer from inputs a test can hand it,
and a thin shim that gets those inputs from the world. The failures these catch are the ones the
internal test round actually produced — an Intel Node winning on PATH, a `claude` CLI that is
installed but signed out, a checkout under ~/Documents that launchd cannot read — so each one
carries the fix rather than just the verdict.

Nothing here installs anything or signs anyone in. It reports; setup asks; the person decides.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from selly_agent import passes, paths
from selly_agent.browser import chrome
from selly_agent.browser import client as browser_client
from selly_agent.installer import checks

# Probes are cheap questions; none of them should ever hang setup.
_PROBE_TIMEOUT_SEC = 30.0
# The Playwright MCP warm-up is a package download on a cold machine, which is minutes, not
# seconds. It is the one slow step here, and it is optional.
_PREWARM_TIMEOUT_SEC = 600.0
# A `brew install` of Node or Chrome on a slow connection.
_BREW_TIMEOUT_SEC = 1800.0

# Playwright MCP needs a modern Node; below this it fails at spawn with an obscure syntax error
# rather than an honest "too old".
NODE_MIN_MAJOR = 18

# Environment variables that mean an agent is running this, not a person. A TTY may well exist —
# an agent session usually has one — so its presence is not evidence of a human, and the
# interactive phases must not be offered to a caller that cannot answer them.
AGENT_ENV_KEYS = ("CLAUDECODE", "CLAUDE_CODE", "AI_AGENT", "SELLY_DAEMON_PASS")


# --- pure ------------------------------------------------------------------------------------


def parse_binary_arch(file_output: str) -> str:
    """Which architectures a Mach-O binary carries, read off `file`'s description."""
    text = (file_output or "").lower()
    has_arm = "arm64" in text
    has_intel = "x86_64" in text
    if "universal binary" in text or (has_arm and has_intel):
        return "universal"
    if has_arm:
        return "arm64"
    if has_intel:
        return "x86_64"
    return "unknown"


def parse_node_major(version_output: str) -> int:
    """The major version out of `node --version` ("v20.11.0" → 20); 0 when unreadable."""
    raw = (version_output or "").strip().lstrip("vV").split(".", 1)[0]
    try:
        return int(raw)
    except ValueError:
        return 0


def parse_auth_status(stdout: str):
    """Whether `claude auth status --json` says a session is signed in.

    Answers True, False, or None for "the output did not say" — which is a distinct outcome from
    "signed out" and must not be reported as one. The payload is located by its first brace
    because the CLI prepends unrelated diagnostics (a missing config file, an update notice) to
    stdout, and a strict json.loads of the whole stream would call a signed-in machine unknown.
    """
    text = stdout or ""
    start = text.find("{")
    if start < 0:
        return None
    try:
        payload = json.loads(text[start:])
    except ValueError:
        return None
    if not isinstance(payload, dict) or "loggedIn" not in payload:
        return None
    return bool(payload["loggedIn"])


def is_tcc_blocked(tree, protected_roots) -> bool:
    """Whether a path sits under a directory macOS gates behind per-app consent.

    A launchd job is never asked for that consent — it is simply refused the read — so an install
    tree in one of these is a daemon that fails on every start with a permissions error nobody is
    prompted about.
    """
    resolved = Path(tree).resolve()
    for root in protected_roots:
        root = Path(root).resolve()
        if resolved == root or root in resolved.parents:
            return True
    return False


def agent_context(env=None) -> str:
    """The name of the environment variable revealing an agent session, or "" for a real shell."""
    env = os.environ if env is None else env
    for key in AGENT_ENV_KEYS:
        if env.get(key):
            return key
    return ""


# --- the world -------------------------------------------------------------------------------


def _run(argv, timeout: float = _PROBE_TIMEOUT_SEC):
    """Run a probe command, answering (returncode, stdout+stderr). Never raises."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def is_apple_silicon() -> bool:
    """Whether the CPU is Apple Silicon — asked of the hardware, not of this process.

    `platform.machine()` reports x86_64 when the interpreter is itself running under Rosetta,
    which is precisely the situation this gate exists to catch: a translated shell whose PATH
    Node is an Intel build. The sysctl answers for the machine either way.
    """
    code, out = _run(["sysctl", "-n", "hw.optional.arm64"])
    return code == 0 and out.strip() == "1"


def binary_arch(path: str) -> str:
    code, out = _run(["file", str(path)])
    return parse_binary_arch(out) if code == 0 else "unknown"


def node_path_fragment() -> str:
    """A PATH fragment reaching both `node` and `npx`, at paths that will still exist tomorrow.

    Directories rather than the binaries' own paths, because `npx` is usually a wrapper whose
    shebang looks `node` up on PATH: naming it absolutely would still leave it unable to find its
    own interpreter.

    Resolved through symlinks rather than taken as `which` reports them: a version manager may hand
    out a per-shell directory — fnm names one after the shell's pid — which stops existing when that
    shell does. What it points at is the installation itself, which persists.

    Usually one directory holds both. When it does not — a global npm prefix carrying `npx` while
    `node` lives elsewhere — both are needed, and node's comes first: whatever `node` the gates
    checked should be the one npx's shebang finds, not a stray build sitting beside npx.

    Empty when either binary is missing; there is nothing to record, and setup's node gate has
    already refused the machine by then.
    """
    directories = []
    for name in ("node", "npx"):
        found = shutil.which(name)
        if not found:
            return ""
        resolved = os.path.realpath(str(Path(found).parent))
        if resolved not in directories:
            directories.append(resolved)
    return ":".join(directories)


def homebrew_path() -> str:
    """Homebrew's binary, or "" when it is not installed.

    Never bootstrapped: installing Homebrew is piping a remote script into a shell, and that is a
    supply-chain decision belonging to whoever owns the machine, not to our installer.
    """
    for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if Path(candidate).is_file():
            return candidate
    return shutil.which("brew") or ""


def brew_install(package: str, *, cask: bool = False) -> tuple:
    """Install one package with Homebrew, answering (ok, output tail). Consent is the caller's."""
    brew = homebrew_path()
    if not brew:
        return False, "Homebrew is not installed"
    argv = [brew, "install"] + (["--cask"] if cask else []) + [package]
    code, out = _run(argv, timeout=_BREW_TIMEOUT_SEC)
    return code == 0, out.strip()[-500:]


# --- gates -----------------------------------------------------------------------------------


def check_platform() -> checks.Check:
    if sys.platform != "darwin":
        return checks.fail(
            "platform",
            f"{sys.platform} is not supported yet",
            "selly-agent runs on macOS today; Windows is a planned port.",
        )
    return checks.ok("platform", "macOS")


def check_tree_location(tree) -> checks.Check:
    """Refuse to install from a tree macOS will not let the daemon read."""
    roots = paths.tcc_protected_roots()
    if is_tcc_blocked(tree, roots):
        names = ", ".join(f"~/{root.name}" for root in roots)
        return checks.fail(
            "install location",
            f"{tree} is under a folder macOS protects ({names})",
            "Move the tree somewhere unprotected (e.g. ~/dev/selly-agent) and re-run ./setup — "
            "the background agent is denied reads there and cannot start.",
        )
    return checks.ok("install location", str(tree))


def check_node() -> checks.Check:
    """Node must be present, recent enough for Playwright MCP, and native to this machine."""
    node = shutil.which("node")
    if not node:
        return checks.fail("node", "not installed", "brew install node")
    code, out = _run([node, "--version"])
    major = parse_node_major(out) if code == 0 else 0
    if major < NODE_MIN_MAJOR:
        found = f"v{major}" if major else "an unreadable version"
        return checks.fail(
            "node",
            f"{node} reports {found}, below the v{NODE_MIN_MAJOR} the browser layer needs",
            "brew install node",
        )
    if is_apple_silicon():
        arch = binary_arch(node)
        if arch not in ("arm64", "universal"):
            return checks.fail(
                "node",
                f"{node} is an {arch} build on an Apple Silicon Mac",
                "That Node runs under Rosetta and the browser server will not start. Install a "
                "native one (`brew install node` with Homebrew in /opt/homebrew) and make sure it "
                "comes first on PATH.",
            )
    if not shutil.which("npx"):
        return checks.fail(
            "node", f"{node} is installed but npx is not on PATH", "brew install node"
        )
    return checks.ok("node", f"v{major} at {node}")


def check_chrome(chrome_bin=None) -> checks.Check:
    binary = chrome.resolve_binary(chrome_bin)
    if not Path(binary).exists():
        return checks.fail(
            "chrome",
            f"not found at {binary}",
            "brew install --cask google-chrome",
        )
    return checks.ok("chrome", binary)


def check_claude(config) -> checks.Check:
    """The harness CLI: installed, and signed in.

    Signed-out-but-installed is the failure the internal test round kept producing, and it is
    invisible until the first pass spawns and dies — so it is a gate here, not a surprise later.
    The status probe is read-only by design: the legacy installer's "run a tiny prompt" check made
    a real model call whose token refresh wrote the keychain, which from a launchd context popped
    a GUI prompt on every probe.
    """
    binary = passes.resolve_claude_bin(config)
    if binary is None:
        return checks.fail(
            "claude CLI",
            "not installed",
            "The `claude` CLI is a separate install from the desktop app: "
            "curl -fsSL https://claude.com/install.sh | bash",
        )
    code, out = _run([binary, "auth", "status", "--json"])
    signed_in = parse_auth_status(out)
    if signed_in is None:
        # Neither a yes nor a no. Fall back to the exit code, which the CLI sets for scripts.
        signed_in = code == 0
    if not signed_in:
        return checks.fail(
            "claude CLI", f"{binary} is installed but signed out", f"{binary} auth login"
        )
    return checks.ok("claude CLI", f"signed in ({binary})")


def claude_login(config) -> int:
    """Hand the terminal to `claude auth login` so the person can sign in without leaving setup.

    Inherits stdio deliberately: this is an interactive OAuth flow that prints a URL and reads a
    pasted code, so capturing its output would hide the very thing they need to act on.
    """
    binary = passes.resolve_claude_bin(config)
    if binary is None:
        return 1
    try:
        return subprocess.call([binary, "auth", "login"])
    except (OSError, subprocess.SubprocessError):
        return 1


def prewarm_playwright(config) -> checks.Check:
    """Resolve the Playwright MCP package now, so the daemon's first browser use is not a
    download. A miss is a warning: it costs latency on the first publish, nothing more."""
    command = config.playwright_mcp_cmd or browser_client.default_command(
        browser_client.cdp_endpoint(config.chrome_cdp_port)
    )
    npx = shutil.which(str(command[0]))
    if not npx:
        return checks.warn("playwright", f"{command[0]} is not on PATH — skipped")
    code, out = _run(
        [npx, "--yes", browser_client.PINNED_MCP_SPEC, "--version"],
        timeout=_PREWARM_TIMEOUT_SEC,
    )
    if code != 0:
        return checks.warn(
            "playwright",
            f"could not pre-resolve {browser_client.PINNED_MCP_SPEC}: {out.strip()[-200:]}",
            "The daemon will fetch it on first use instead — the first browser listing will be "
            "slower.",
        )
    return checks.ok("playwright", f"{browser_client.PINNED_MCP_SPEC} resolved")
