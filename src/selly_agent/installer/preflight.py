"""The machine gates setup runs before it writes anything.

Each gate is split in two: a pure function that decides an answer from inputs a test can hand it,
and a thin shim that gets those inputs from the world. The failures these catch are the ones the
internal test round actually produced — an Intel Node winning on PATH, a `claude` CLI that is
installed but signed out, a checkout under ~/Documents that launchd cannot read — so each one
carries the fix rather than just the verdict.

Nothing here installs anything or signs anyone in. It reports; setup asks; the person decides.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from selly_agent import passes, paths, supervisor
from selly_agent.browser import chrome
from selly_agent.browser import client as browser_client
from selly_agent.installer import checks, runtime

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


def _run(argv, timeout: float = _PROBE_TIMEOUT_SEC, env=None):
    """Run a probe command, answering (returncode, stdout+stderr). Never raises.

    `env` replaces the environment outright rather than extending it — the probes that pass one are
    the ones asking what the supervised worker can do, and inheriting this shell's would answer for
    the wrong machine.
    """
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=env,
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
    if sys.platform != "darwin":
        return False  # the question is about Rosetta, which nothing else has
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
    return os.pathsep.join(directories)


def supervised_path(fragment: str | None = None) -> str:
    """The PATH the supervised worker will actually have: the recorded fragment, then the default.

    The same join the supervisor writes into the job definition, so a check run against this is
    checking the real thing rather than an approximation of it.
    """
    fragment = node_path_fragment() if fragment is None else fragment
    if not fragment:
        return supervisor.SUPERVISED_PATH
    return f"{fragment}{os.pathsep}{supervisor.SUPERVISED_PATH}"


def supervised_env(fragment: str | None = None) -> dict:
    """The environment to run a supervised-worker check under.

    Near-empty, because that is roughly what a supervisor hands the job — and deliberately not this
    shell's environment, which carries a version manager's shims the worker will never see. What
    counts as the bare minimum differs per platform, so paths answers that.
    """
    return {"PATH": supervised_path(fragment), **paths.supervised_env_base()}


# What each dependency is called to the tool that installs it, and how that tool is named to a
# person. macOS gets Homebrew, Windows gets winget — which ships with Windows, so unlike Homebrew
# there is nothing to bootstrap before it can be offered.
_PACKAGE_MANAGERS = {
    "darwin": {
        "name": "Homebrew",
        "packages": {"node": ("node", False), "chrome": ("google-chrome", True)},
    },
    "win32": {
        "name": "winget",
        "packages": {"node": ("OpenJS.NodeJS.LTS", False), "chrome": ("Google.Chrome", False)},
    },
}


def setup_door() -> str:
    """How a person re-runs setup, spelled for this platform's front door."""
    return r".\setup.ps1" if os.name == "nt" else "./setup"


def package_manager_name() -> str:
    """What to call the installer this platform uses, or "" where we know of none."""
    return _PACKAGE_MANAGERS.get(sys.platform, {}).get("name", "")


def install_command(dependency: str) -> list:
    """The argv that installs `dependency`, or [] when this platform has no manager for it.

    One place so a gate's remediation text and the command setup actually runs cannot disagree,
    which is the way a person ends up pasting something that does not work.
    """
    manager = _PACKAGE_MANAGERS.get(sys.platform)
    if manager is None:
        return []
    entry = manager["packages"].get(dependency)
    if entry is None:
        return []
    package, cask = entry
    if sys.platform == "win32":
        return ["winget", "install", "--exact", "--id", package]
    brew = homebrew_path()
    if not brew:
        return []
    return [brew, "install", *(["--cask"] if cask else []), package]


def install_hint(dependency: str) -> str:
    """The remediation line for a missing dependency: the command, if there is one."""
    argv = install_command(dependency)
    return " ".join(argv) if argv else f"Install {dependency} and re-run {setup_door()}."


def _claude_install_hint() -> str:
    """How to get the `claude` CLI, spelled for this platform.

    The published one-liner is a shell script, so it is only offered where there is a shell to
    run it in; elsewhere this falls back to the generic hint rather than naming a command that
    would fail. The CLI is a separate install from the desktop app either way, which is the part
    people get wrong.
    """
    if os.name == "nt":
        return (
            "The `claude` CLI is a separate install from the desktop app. Install it, then "
            f"re-run {setup_door()}."
        )
    return (
        "The `claude` CLI is a separate install from the desktop app: "
        "curl -fsSL https://claude.com/install.sh | bash"
    )


def install_dependency(dependency: str) -> tuple:
    """Install one dependency with this platform's manager, answering (ok, output tail).

    Consent is the caller's business, not this function's.
    """
    argv = install_command(dependency)
    if not argv:
        return False, f"{package_manager_name() or 'a package manager'} is not available"
    code, out = _run(argv, timeout=_BREW_TIMEOUT_SEC)
    return code == 0, out.strip()[-500:]


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


_PLATFORM_NAMES = {"darwin": "macOS", "win32": "Windows"}


def check_platform() -> checks.Check:
    name = _PLATFORM_NAMES.get(sys.platform)
    if name is None:
        return checks.fail(
            "platform",
            f"{sys.platform} is not supported yet",
            "selly-agent runs on macOS and Windows today; Linux is a planned port.",
        )
    return checks.ok("platform", name)


def check_runtime(tree) -> checks.Check:
    """That this tree's dependencies are actually installed and importable.

    No system-Python check sits beside this one, because there is nothing to check: the front
    door provisions the interpreter. What can still be wrong is the venv — a deleted directory,
    an interrupted sync — and the honest way to ask is to import a dependency rather than to look
    for a directory and assume.
    """
    report = runtime.describe(tree)
    if not report["present"]:
        return checks.fail(
            "python runtime",
            "this install has no dependency environment",
            f"Re-run {setup_door()} — it provisions the interpreter and installs dependencies.",
        )
    if not report["dependencies_importable"]:
        return checks.fail(
            "python runtime",
            f"dependencies are not importable ({report['detail'] or 'unknown error'})",
            f"Re-run {setup_door()} to reinstall them.",
        )
    return checks.ok("python runtime", report["interpreter"])


def check_state_store() -> checks.Check:
    """That a SQLite database in WAL mode can live where the store does.

    WAL keeps shared-memory files beside the database, which a folder-redirected profile — a
    network share, a sync service — can refuse even where plain files write fine. A broken store
    must never be a quiet store, so this fails setup rather than the first write.
    """
    target = paths.state_dir()
    probe = target / ".preflight-wal-probe.sqlite3"
    fix = (
        "The state directory must be on a local disk — a redirected or synced profile cannot "
        "hold the agent's live databases."
    )
    try:
        target.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(probe))
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            conn.execute("CREATE TABLE probe (x)")
            conn.execute("INSERT INTO probe VALUES (1)")
            conn.commit()
        finally:
            conn.close()
        if str(mode).lower() != "wal":
            return checks.fail(
                "state store",
                f"{target} cannot hold a WAL database (journal mode came back {mode!r})",
                fix,
            )
    except (OSError, sqlite3.Error) as exc:
        return checks.fail("state store", f"cannot write a database under {target}: {exc}", fix)
    finally:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(f"{probe}{suffix}")
    return checks.ok("state store", f"WAL database writable under {target}")


def check_tree_location(tree) -> checks.Check:
    """Refuse to install from a tree the daemon will not be allowed to read.

    macOS-shaped, and passes everywhere else by construction: the protected roots are empty off
    macOS, so there is nothing for a tree to be under.
    """
    roots = paths.tcc_protected_roots()
    if is_tcc_blocked(tree, roots):
        names = ", ".join(f"~/{root.name}" for root in roots)
        return checks.fail(
            "install location",
            f"{tree} is under a folder macOS protects ({names})",
            f"Move the tree somewhere unprotected (e.g. ~/dev/selly-agent) and re-run "
            f"{setup_door()} — the background agent is denied reads there and cannot start.",
        )
    return checks.ok("install location", str(tree))


def check_node() -> checks.Check:
    """Node must be present, recent enough for Playwright MCP, and native to this machine."""
    node = shutil.which("node")
    if not node:
        return checks.fail("node", "not installed", install_hint("node"))
    code, out = _run([node, "--version"])
    major = parse_node_major(out) if code == 0 else 0
    if major < NODE_MIN_MAJOR:
        found = f"v{major}" if major else "an unreadable version"
        return checks.fail(
            "node",
            f"{node} reports {found}, below the v{NODE_MIN_MAJOR} the browser layer needs",
            install_hint("node"),
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
            "node", f"{node} is installed but npx is not on PATH", install_hint("node")
        )
    return checks.ok("node", f"v{major} at {node}")


def check_chrome(chrome_bin=None) -> checks.Check:
    binary = chrome.resolve_binary(chrome_bin)
    if not Path(binary).exists():
        return checks.fail(
            "chrome",
            f"not found at {binary}",
            install_hint("chrome"),
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
            _claude_install_hint(),
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


def check_supervised_spawn(config) -> checks.Check:
    """Can the browser server be spawned by the *worker*, not just by this shell?

    Every other node check here asks the question of the interactive shell that ran setup, whose
    PATH carries a version manager's shims. The worker's does not: it gets the recorded fragment
    plus a minimal default and nothing else. So this runs the same binaries the daemon will run,
    under the same PATH the supervisor is about to write into the job definition.

    It is fatal on purpose. Whichever node layout we failed to anticipate becomes a setup failure
    with a message here, instead of a browser lane that is silently dead at the first publish. And
    it can afford to be fatal because it needs no network — nothing about it can be tripped by a
    hiccup.
    """
    command = config.playwright_mcp_cmd or browser_client.default_command(
        browser_client.cdp_endpoint(config.chrome_cdp_port)
    )
    fragment = node_path_fragment()
    path = supervised_path(fragment)
    env = supervised_env(fragment)

    # The override's own binary is whatever it names; the default's is `npx`, which spawns `node`
    # through its shebang — so both have to answer under that PATH, not just the one we invoke.
    fix = (
        "The worker is started with that PATH and nothing else. Install Node so that `node` and "
        f"`npx` sit in directories setup can record, then re-run {setup_door()}."
    )
    probes = [[str(command[0]), "--version"]]
    if not config.playwright_mcp_cmd:
        probes.insert(0, ["node", "--version"])
    for probe in probes:
        # Resolved against the worker's PATH the way the daemon resolves at spawn time. A bare
        # name would ask the wrong question twice on Windows: process creation ignores PATHEXT
        # (so `npx` never finds npx.cmd even when it is right there), and a spawn with env=
        # replaced still searches the *caller's* PATH rather than the passed one.
        resolved = shutil.which(probe[0], path=env.get("PATH"))
        if resolved is None:
            return checks.fail(
                "browser server",
                f"`{probe[0]}` is not on the background worker's PATH ({path})",
                fix,
            )
        code, out = _run([resolved, *probe[1:]], env=env)
        if code != 0:
            return checks.fail(
                "browser server",
                f"`{' '.join(probe)}` does not run under the background worker's PATH "
                f"({path}): {out.strip()[-200:]}",
                fix,
            )
    return checks.ok("browser server", f"spawns under the worker's PATH ({path})")


def prewarm_playwright(config) -> checks.Check:
    """Resolve the Playwright MCP package now, so the daemon's first browser use is not a
    download. A miss is a warning: it costs latency on the first publish, nothing more."""
    command = config.playwright_mcp_cmd or browser_client.default_command(
        browser_client.cdp_endpoint(config.chrome_cdp_port)
    )
    npx = shutil.which(str(command[0]))
    if not npx:
        return checks.warn("playwright", f"{command[0]} is not on PATH — skipped")
    # Under the worker's environment, like the gate above: the download this fills is the one the
    # worker will look for, and npm keys its cache off HOME, which that environment keeps.
    code, out = _run(
        [npx, "--yes", browser_client.PINNED_MCP_SPEC, "--version"],
        timeout=_PREWARM_TIMEOUT_SEC,
        env=supervised_env(),
    )
    if code != 0:
        return checks.warn(
            "playwright",
            f"could not pre-resolve {browser_client.PINNED_MCP_SPEC}: {out.strip()[-200:]}",
            "The daemon will fetch it on first use instead — the first browser listing will be "
            "slower.",
        )
    return checks.ok("playwright", f"{browser_client.PINNED_MCP_SPEC} resolved")
