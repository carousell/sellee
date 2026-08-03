"""Preflight gates: the pure decisions with fake inputs, and the probes with a faked world."""

from __future__ import annotations

from pathlib import Path

from selly_agent.config import Config
from selly_agent.installer import checks, preflight

# --- pure decisions ---------------------------------------------------------------------------


def test_binary_arch_reads_file_output() -> None:
    assert preflight.parse_binary_arch("Mach-O 64-bit executable arm64") == "arm64"
    assert preflight.parse_binary_arch("Mach-O 64-bit executable x86_64") == "x86_64"
    assert (
        preflight.parse_binary_arch("Mach-O universal binary with 2 architectures") == "universal"
    )
    assert preflight.parse_binary_arch("Mach-O 64-bit x86_64 ... and arm64 ...") == "universal"
    assert preflight.parse_binary_arch("ASCII text") == "unknown"
    assert preflight.parse_binary_arch("") == "unknown"


def test_node_major_parses_a_version_string() -> None:
    assert preflight.parse_node_major("v20.11.0\n") == 20
    assert preflight.parse_node_major("18.0.0") == 18
    assert preflight.parse_node_major("not a version") == 0
    assert preflight.parse_node_major("") == 0


def test_auth_status_reads_the_json_payload() -> None:
    assert preflight.parse_auth_status('{"loggedIn": true, "authMethod": "claude.ai"}') is True
    assert preflight.parse_auth_status('{"loggedIn": false}') is False


def test_auth_status_finds_the_payload_behind_cli_chatter() -> None:
    # The CLI prepends unrelated diagnostics to stdout; a strict parse of the whole stream would
    # report a signed-in machine as unknown.
    noisy = 'Claude configuration file not found at: /x/.claude.json\n\n{"loggedIn": true}\n'
    assert preflight.parse_auth_status(noisy) is True


def test_auth_status_says_unknown_rather_than_signed_out() -> None:
    assert preflight.parse_auth_status("") is None
    assert preflight.parse_auth_status("command not found") is None
    assert preflight.parse_auth_status("{not json") is None
    assert preflight.parse_auth_status('{"other": 1}') is None


def test_tcc_gate_covers_the_protected_roots_and_their_contents() -> None:
    home = Path("/Users/someone")
    roots = [home / "Documents", home / "Desktop", home / "Downloads"]
    assert preflight.is_tcc_blocked(home / "Documents" / "selly-agent", roots) is True
    assert preflight.is_tcc_blocked(home / "Documents", roots) is True
    assert preflight.is_tcc_blocked(home / "dev" / "selly-agent", roots) is False


def test_agent_context_names_the_variable_that_gave_it_away() -> None:
    assert preflight.agent_context({"CLAUDECODE": "1"}) == "CLAUDECODE"
    assert preflight.agent_context({"AI_AGENT": "yes"}) == "AI_AGENT"
    assert preflight.agent_context({"CLAUDECODE": ""}) == ""
    assert preflight.agent_context({"TERM": "xterm"}) == ""


# --- gates over a faked world -----------------------------------------------------------------


def test_tree_location_gate_refuses_a_protected_folder(monkeypatch, tmp_path) -> None:
    protected = tmp_path / "Documents"
    (protected / "selly-agent").mkdir(parents=True)
    monkeypatch.setattr(preflight.paths, "tcc_protected_roots", lambda: [protected])
    result = preflight.check_tree_location(protected / "selly-agent")
    assert result.status == checks.FAIL
    assert "Documents" in result.detail
    assert "Move the tree" in result.fix


def test_tree_location_gate_accepts_an_ordinary_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(preflight.paths, "tcc_protected_roots", lambda: [tmp_path / "Documents"])
    assert preflight.check_tree_location(tmp_path / "dev").status == checks.OK


def test_node_gate_reports_a_missing_node(monkeypatch) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    result = preflight.check_node()
    assert result.status == checks.FAIL
    assert result.fix == "brew install node"


def test_node_gate_refuses_an_intel_node_on_apple_silicon(monkeypatch) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(preflight, "_run", lambda argv, timeout=30.0: (0, "v20.11.0"))
    monkeypatch.setattr(preflight, "is_apple_silicon", lambda: True)
    monkeypatch.setattr(preflight, "binary_arch", lambda path: "x86_64")
    result = preflight.check_node()
    assert result.status == checks.FAIL
    assert "Rosetta" in result.fix


def test_node_gate_accepts_a_universal_node_on_apple_silicon(monkeypatch) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda name: f"/opt/homebrew/bin/{name}")
    monkeypatch.setattr(preflight, "_run", lambda argv, timeout=30.0: (0, "v22.1.0"))
    monkeypatch.setattr(preflight, "is_apple_silicon", lambda: True)
    monkeypatch.setattr(preflight, "binary_arch", lambda path: "universal")
    assert preflight.check_node().status == checks.OK


def test_node_gate_refuses_a_version_below_the_floor(monkeypatch) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(preflight, "_run", lambda argv, timeout=30.0: (0, "v16.0.0"))
    monkeypatch.setattr(preflight, "is_apple_silicon", lambda: False)
    result = preflight.check_node()
    assert result.status == checks.FAIL
    assert "v18" in result.detail


def test_node_gate_refuses_node_without_npx(monkeypatch) -> None:
    monkeypatch.setattr(
        preflight.shutil, "which", lambda name: "/usr/local/bin/node" if name == "node" else None
    )
    monkeypatch.setattr(preflight, "_run", lambda argv, timeout=30.0: (0, "v20.0.0"))
    monkeypatch.setattr(preflight, "is_apple_silicon", lambda: False)
    result = preflight.check_node()
    assert result.status == checks.FAIL
    assert "npx" in result.detail


def test_chrome_gate_checks_the_binary_the_launch_would_use(tmp_path) -> None:
    binary = tmp_path / "Chrome"
    assert preflight.check_chrome(str(binary)).status == checks.FAIL
    binary.write_text("")
    assert preflight.check_chrome(str(binary)).status == checks.OK


def test_claude_gate_reports_installed_but_signed_out(monkeypatch) -> None:
    monkeypatch.setattr(preflight.passes, "resolve_claude_bin", lambda cfg: "/bin/claude")
    monkeypatch.setattr(preflight, "_run", lambda argv, timeout=30.0: (1, '{"loggedIn": false}'))
    result = preflight.check_claude(Config())
    assert result.status == checks.FAIL
    assert result.fix == "/bin/claude auth login"


def test_claude_gate_reports_a_missing_cli(monkeypatch) -> None:
    monkeypatch.setattr(preflight.passes, "resolve_claude_bin", lambda cfg: None)
    result = preflight.check_claude(Config())
    assert result.status == checks.FAIL
    assert "separate install" in result.fix


def test_claude_gate_passes_when_signed_in(monkeypatch) -> None:
    monkeypatch.setattr(preflight.passes, "resolve_claude_bin", lambda cfg: "/bin/claude")
    monkeypatch.setattr(preflight, "_run", lambda argv, timeout=30.0: (0, '{"loggedIn": true}'))
    assert preflight.check_claude(Config()).status == checks.OK


def test_claude_gate_falls_back_to_the_exit_code_when_the_output_is_unreadable(monkeypatch) -> None:
    monkeypatch.setattr(preflight.passes, "resolve_claude_bin", lambda cfg: "/bin/claude")
    monkeypatch.setattr(preflight, "_run", lambda argv, timeout=30.0: (0, "no json here"))
    assert preflight.check_claude(Config()).status == checks.OK


def test_prewarm_is_a_warning_not_a_failure(monkeypatch) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/npx")
    monkeypatch.setattr(preflight, "_run", lambda argv, timeout=None: (1, "network unreachable"))
    result = preflight.prewarm_playwright(Config())
    assert result.status == checks.WARN


def test_homebrew_is_never_bootstrapped(monkeypatch) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    monkeypatch.setattr(preflight.Path, "is_file", lambda self: False)
    ok, detail = preflight.brew_install("node")
    assert ok is False
    assert "Homebrew is not installed" in detail


# --- the fail-open contract ---------------------------------------------------------------------


def test_a_probe_that_raises_becomes_a_failed_check_not_a_crash() -> None:
    def explode():
        raise RuntimeError("boom")

    result = checks.fail_open("chrome", explode, fix="try again")
    assert result.status == checks.FAIL
    assert "RuntimeError" in result.detail
    assert result.fix == "try again"


def test_render_and_exit_code() -> None:
    rendered = checks.render(
        [
            checks.ok("daemon", "running"),
            checks.warn("channel", "not bound", "selly-agent connect telegram"),
        ]
    )
    assert rendered[0].startswith("✅ daemon: running")
    assert rendered[-1] == "   → selly-agent connect telegram"
    assert checks.exit_code([checks.ok("a", "b"), checks.warn("c", "d")]) == 0
    assert checks.exit_code([checks.ok("a", "b"), checks.fail("c", "d")]) == 1


def test_the_node_directory_is_recorded_at_a_path_that_outlives_the_shell(
    tmp_path, monkeypatch
) -> None:
    # fnm hands out a directory named after the shell's pid, which is gone once that shell is.
    # Recording it verbatim would leave the worker pointed at nothing after the next logout.
    installation = tmp_path / "node-versions" / "v22" / "installation"
    (installation / "bin").mkdir(parents=True)
    for name in ("node", "npx"):
        (installation / "bin" / name).write_text("#!/bin/sh\n")
    per_shell = tmp_path / "fnm_multishells" / "52166_1785491228033"
    per_shell.parent.mkdir(parents=True)
    per_shell.symlink_to(installation)

    monkeypatch.setattr(preflight.shutil, "which", lambda name: str(per_shell / "bin" / name))

    # One directory holds both, so the fragment is a single entry — the plist stays as it was.
    assert preflight.node_path_fragment() == str(installation / "bin")


def test_a_divergent_npx_records_both_directories_with_node_first(tmp_path, monkeypatch) -> None:
    # A global npm prefix can carry `npx` while `node` lives elsewhere. Recording only npx's
    # directory passed setup and then failed under the supervisor, where nothing else is on PATH.
    node_dir = tmp_path / "node" / "bin"
    npx_dir = tmp_path / "npm-global" / "bin"
    for directory in (node_dir, npx_dir):
        directory.mkdir(parents=True)
    binaries = {"node": str(node_dir / "node"), "npx": str(npx_dir / "npx")}
    monkeypatch.setattr(preflight.shutil, "which", lambda name: binaries.get(name))

    # Node first: npx's shebang looks `node` up on PATH, and the node the gates checked must win
    # over any stray build sitting beside npx.
    assert preflight.node_path_fragment() == f"{node_dir}:{npx_dir}"


def test_a_missing_binary_records_nothing_rather_than_a_guess(monkeypatch) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    assert preflight.node_path_fragment() == ""

    # npx alone is not enough: the fragment exists to reach both.
    monkeypatch.setattr(
        preflight.shutil, "which", lambda name: "/usr/local/bin/npx" if name == "npx" else None
    )
    assert preflight.node_path_fragment() == ""
