"""The deployment marker, and the two things the container profile must NOT change.

The marker itself is three lines of code, so most of what is worth pinning here is the shape of
its absence: an unset, empty or misspelled value is a host install, because host is what every
other branch in the tree assumes.

The rest of this file is a pair of regression pins. The container reaches the seller's Chrome
through a forwarder that makes the CDP endpoint loopback-shaped on both sides, which is what
keeps the pass argv, the workspace's .mcp.json and both round-trip validators identical to a
host install's. A future "helpful" host knob would silently break Chrome's own DNS-rebinding
refusal, so the sameness is asserted rather than assumed.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from selly_agent import daemon, deployment, passes, paths
from selly_agent.config import Config
from selly_agent.harness import claude
from selly_agent.harness.model import PassSpec, StdioServer

# --- the marker ---------------------------------------------------------------------------------


def test_no_marker_is_a_host_install() -> None:
    assert deployment.profile({}) == deployment.HOST
    assert not deployment.is_container({})
    assert deployment.manages_chrome({})


def test_the_marker_selects_the_container_profile() -> None:
    env = {deployment.MARKER_VAR: "container"}
    assert deployment.profile(env) == deployment.CONTAINER
    assert deployment.is_container(env)
    # The one substantive consequence: the browser is not ours to start.
    assert not deployment.manages_chrome(env)


def test_anything_that_is_not_the_marker_reads_as_a_host_install() -> None:
    """A typo must not land somewhere that supervises nothing and launches nothing."""
    for value in ("", "  ", "docker", "containers", "host", "1"):
        assert deployment.profile({deployment.MARKER_VAR: value}) == deployment.HOST


def test_the_marker_is_case_and_whitespace_forgiving() -> None:
    assert deployment.is_container({deployment.MARKER_VAR: " Container\n"})


def test_the_env_is_read_at_call_time(monkeypatch) -> None:
    """Never captured at import: the daemon, the CLI and the tests all resolve it live."""
    assert not deployment.is_container()
    monkeypatch.setenv(deployment.MARKER_VAR, deployment.CONTAINER)
    assert deployment.is_container()


# --- no container-engine vocabulary -----------------------------------------------------------


# Names of container engines and their commands. Fine in a comment or a docstring, where they
# explain the rule; never in a string the program can print.
ENGINE_WORDS = ("docker", "podman", "kubectl", "compose up", "compose down")

SRC = Path(deployment.__file__).resolve().parent


def _docstring_nodes(tree):
    """Every node whose first statement is a docstring, so its text can be exempted."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                yield first.value


def test_no_runtime_string_names_a_container_engine() -> None:
    """We ship a compose file and the docs name its commands, but the program must not.

    Which engine is running it, and what the container is called, are the operator's business:
    `podman`, a hand-written `docker run`, something we have never heard of. A message printing
    `docker exec -it selly-agent …` at someone whose setup is none of those is worse than one
    saying "in the container" and letting them translate.
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        exempt = {id(node) for node in _docstring_nodes(tree)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in exempt:
                continue
            found = [word for word in ENGINE_WORDS if word in node.value.lower()]
            if found:
                offenders.append(f"{path.name}:{node.lineno}: {found}")
    assert not offenders, (
        'container-engine vocabulary in a runtime string (say "the container" instead):\n'
        + "\n".join(offenders)
    )


# --- the CDP endpoint stays loopback ------------------------------------------------------------


def _browser_spec(command) -> PassSpec:
    return PassSpec(
        prompt="publish item item_123 using only your tools",
        model="sonnet",
        mcp_endpoint="http://127.0.0.1:7355/mcp",
        mcp_token="TESTTOKEN",
        allowed_tools=("mcp__selly__get_item",),
        max_turns=20,
        browser_server=StdioServer(
            name="playwright",
            command=command[0],
            args=tuple(command[1:]),
            tools=("browser_navigate",),
        ),
    )


def test_the_pass_browser_command_is_loopback_in_a_container_too(container) -> None:
    """The forwarder puts the endpoint on the container's own loopback, so nothing here moves —
    and a host knob added later would break Chrome's DNS-rebinding refusal on the /json routes."""
    command = passes.browser_command(Config(chrome_cdp_port=9222))
    assert "--cdp-endpoint" in command
    assert command[command.index("--cdp-endpoint") + 1] == "http://127.0.0.1:9222"


def test_the_workspace_mcp_config_is_loopback_in_a_container_too(container) -> None:
    command = passes.browser_command(Config(chrome_cdp_port=9222))
    rendered = json.dumps(claude.mcp_config(_browser_spec(command)))
    assert "http://127.0.0.1:9222" in rendered
    assert "host.docker.internal" not in rendered


# --- where the daemon's own server binds ---------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "expected"),
    [(None, "127.0.0.1"), ("", "127.0.0.1"), ("  ", "127.0.0.1"), ("0.0.0.0", "0.0.0.0")],
)
def test_the_bind_host_is_loopback_unless_the_environment_widens_it(
    xdg_tmp, monkeypatch, env, expected
) -> None:
    """The image sets 0.0.0.0 so a published port has something to reach. Everywhere else the
    default holds, including when the variable is present but says nothing."""
    monkeypatch.delenv("SELLY_BIND_HOST", raising=False)
    if env is not None:
        monkeypatch.setenv("SELLY_BIND_HOST", env)
    paths.ensure_config_dir()
    paths.config_path().write_text(json.dumps({"http_port": 0}))

    recorded = {}
    real = daemon.HttpServer
    monkeypatch.setattr(
        daemon, "HttpServer", lambda **kwargs: (recorded.update(kwargs), real(**kwargs))[1]
    )
    assert daemon.run_daemon(once=True) == 0
    assert recorded["host"] == expected
