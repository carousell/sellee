"""The image, the compose files and the host Chrome scripts, checked as far as text can be.

Nothing here builds an image — that needs a Docker daemon, and the things that would actually go
wrong (whether the container can reach a loopback-bound Chrome on the host, whether a publish
transfers photos) need a real machine with a real browser besides. What a test suite *can* hold
is the set of facts that are stated in two places at once, where one copy drifting is silent:
the launch flags, the pinned browser-server version, the marker, and the loopback rule.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sellee import deployment
from sellee.browser import chrome, client

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE = (ROOT / "compose.yaml").read_text()
COMPOSE_LINUX = (ROOT / "compose.linux.yaml").read_text()
ENTRYPOINT = (ROOT / "docker" / "entrypoint.sh").read_text()
START_SH = (ROOT / "start-chrome.sh").read_text()
START_PS1 = (ROOT / "start-chrome.ps1").read_text()


def _flags(text: str) -> set:
    """The Chrome switch names a launcher passes, ignoring their values."""
    return {match.split("=")[0] for match in re.findall(r"--[a-z][a-z-]+(?:=[^\s\"',]*)?", text)}


# --- the host Chrome scripts ------------------------------------------------------------------


@pytest.mark.parametrize("script", [START_SH, START_PS1], ids=["sh", "ps1"])
def test_the_launch_scripts_pass_the_same_switches_as_the_daemon_would(script) -> None:
    """A host install and a container drive the same browser the same way. If these drift, the
    difference shows up as a behavioural mystery on one profile and not the other."""
    expected = _flags(" ".join(chrome.launch_command(9222)))
    assert expected <= _flags(script)


@pytest.mark.parametrize("script", [START_SH, START_PS1], ids=["sh", "ps1"])
def test_the_launch_scripts_never_put_cdp_on_a_network_interface(script) -> None:
    """The debugging port is browser control with no authentication of any kind. It stays on
    loopback; the container reaches it through a forwarder, not by it being exposed."""
    assert "--remote-debugging-address" not in script


@pytest.mark.parametrize("script", [START_SH, START_PS1], ids=["sh", "ps1"])
def test_the_launch_scripts_use_a_profile_of_the_seller_s_own(script) -> None:
    """Never the everyday Chrome — and never the container's profile directory either, which
    stays empty in this profile."""
    assert "--user-data-dir" in script
    assert "/data/share" not in script


# --- the image ----------------------------------------------------------------------------------


def test_the_image_carries_the_deployment_marker() -> None:
    assert f"ENV {deployment.MARKER_VAR}={deployment.CONTAINER}" in DOCKERFILE


def test_the_image_points_all_four_xdg_roots_at_the_one_bind_mount() -> None:
    for var in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        assert f"{var}=/data/" in DOCKERFILE


def test_the_image_does_not_restate_the_pinned_browser_server_version() -> None:
    """It reads the spec out of the code at build time. A second copy here would go stale, and a
    stale one is invisible: the daemon would just download the real pin on the first browser
    action instead of using the cache this image warmed."""
    assert client.MCP_VERSION not in DOCKERFILE
    assert "PINNED_MCP_SPEC" in DOCKERFILE


def test_the_image_installs_what_the_daemon_shells_out_to() -> None:
    # `ps` for the pass runner's process tree, socat for the CDP forwarder, tini to reap the node
    # processes a pass orphans, tzdata for the clock. Photo conversion is not here: it is a
    # bundled Python dependency rather than a system tool.
    for package in ("procps", "socat", "tini", "tzdata"):
        assert package in DOCKERFILE
    assert "imagemagick" not in DOCKERFILE


def test_the_image_pins_the_harness_it_installs() -> None:
    assert re.search(r"@anthropic-ai/claude-code@\$\{CLAUDE_CODE_VERSION\}", DOCKERFILE)
    assert re.search(r"ARG CLAUDE_CODE_VERSION=\d+\.\d+\.\d+", DOCKERFILE)


def test_the_secrets_file_never_enters_the_build_context() -> None:
    """docs point the seller at a .env beside compose.yaml, and `COPY . /opt/sellee` would
    otherwise bake their harness token into an image layer — which deleting the file afterwards
    does not undo, and which travels with any export of that image."""
    assert "\n.env\n" in (ROOT / ".dockerignore").read_text()
    assert "\n.env\n" in (ROOT / ".gitignore").read_text()


def test_the_data_directory_never_enters_the_build_context_or_a_commit() -> None:
    """Same reasoning as the secrets file, but the default sits inside the clone and the documented
    update flow is `git pull` in that tree. It was ignored by the build and not by git."""
    assert "\nsellee-data\n" in (ROOT / ".dockerignore").read_text()
    assert "\nsellee-data/\n" in (ROOT / ".gitignore").read_text()


@pytest.mark.parametrize(
    "script", [ENTRYPOINT, START_SH, START_PS1], ids=["entrypoint", "sh", "ps1"]
)
def test_no_shipped_script_creates_a_world_writable_path(script) -> None:
    """The entrypoint once chmod'd a directory 0777 inside the seller's bind mount — no sticky
    bit, so any other local account could replace files another user put there, and the fix was
    lost in a rebase once without anything noticing. These scripts run outside paths.py's
    authority, so the mode discipline has to be asserted at the text level."""
    for mode in re.findall(r"(?:chmod|mkdir\s+-m)\s+([0-7]{3,4})", script):
        assert int(mode[-1]) & 0o2 == 0, f"world-writable mode {mode}"
    assert "a+w" not in script
    assert "o+w" not in script


def test_shell_scripts_are_pinned_to_lf() -> None:
    """A CRLF entrypoint dies inside the image as `/bin/sh^M: bad interpreter`."""
    attributes = (ROOT / ".gitattributes").read_text()
    assert "*.sh text eol=lf" in attributes


def test_tini_is_pid_one() -> None:
    assert DOCKERFILE.count("ENTRYPOINT") == 1
    assert '"/usr/bin/tini"' in DOCKERFILE


# --- the forwarder --------------------------------------------------------------------------


def test_the_forwarder_listens_on_loopback_only() -> None:
    """Chrome refuses a /json request whose Host header is a DNS name, and answers with a
    loopback WebSocket URL that Playwright then dials verbatim — so the endpoint has to be
    loopback inside the container as well as outside it."""
    assert "bind=127.0.0.1" in ENTRYPOINT
    assert "host.docker.internal" in ENTRYPOINT


def test_a_linux_host_swaps_the_forwarder_for_the_host_network() -> None:
    assert "network_mode: host" in COMPOSE_LINUX
    assert 'SELLEE_CDP_FORWARD: "0"' in COMPOSE_LINUX


# --- compose --------------------------------------------------------------------------------


def test_the_container_refuses_to_start_without_a_timezone_or_a_token() -> None:
    """Both absences are silent: a UTC clock moves quiet hours, having neither credential dies at
    the first pass. The entrypoint checks, not compose — compose interpolates on every command,
    so a `${VAR:?}` here would refuse to build an image containing none of these values."""
    assert "TZ: ${TZ:-}" in COMPOSE
    assert "CLAUDE_CODE_OAUTH_TOKEN: ${CLAUDE_CODE_OAUTH_TOKEN:-}" in COMPOSE
    assert "ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}" in COMPOSE
    directives = [line for line in COMPOSE.splitlines() if not line.strip().startswith("#")]
    assert ":?" not in "\n".join(directives)

    assert "error: TZ is unset" in ENTRYPOINT
    assert "error: neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY is set" in ENTRYPOINT
    assert "exit 1" in ENTRYPOINT


def test_setting_both_credentials_warns_which_one_wins() -> None:
    """The both-set case happens by accident: compose reads the host shell's environment before
    .env, so an ANTHROPIC_API_KEY exported in the seller's shell silently outbids the token they
    put in .env — and the claude CLI prefers the API key, moving billing from their subscription
    to per-token Console billing. The entrypoint names the winner instead of leaving it to the
    invoice."""
    assert "both CLAUDE_CODE_OAUTH_TOKEN and ANTHROPIC_API_KEY are set" in ENTRYPOINT
    assert "will use ANTHROPIC_API_KEY" in ENTRYPOINT


def test_the_one_published_port_is_scoped_to_host_loopback() -> None:
    """The web tail shares its server with the MCP and control surface, so publishing it puts all
    three within reach. `"7355:7355"` — no host address — would put them on the seller's LAN."""
    published = re.findall(r"^\s+- \"([^\"]+)\"", COMPOSE, re.MULTILINE)
    assert published == ["127.0.0.1:7355:7355"]
    # Host networking discards published ports anyway, and the loopback it shares is the host's.
    assert "ports:" not in COMPOSE_LINUX


def test_a_container_binds_wide_enough_for_that_port_to_reach_it() -> None:
    """A published port arrives on the bridge address, so a loopback-only bind answers nothing."""
    assert "ENV SELLEE_BIND_HOST=0.0.0.0" in DOCKERFILE


def test_sharing_a_host_s_network_namespace_puts_the_bind_back_on_loopback() -> None:
    """0.0.0.0 there is the machine's real interfaces, and the control surface with it."""
    assert "SELLEE_BIND_HOST: 127.0.0.1" in COMPOSE_LINUX


def test_compose_mounts_one_directory_and_lets_docker_supervise() -> None:
    assert ":/data" in COMPOSE
    assert "restart: unless-stopped" in COMPOSE
    # `docker stop` is a SIGTERM the daemon drains on; killing it mid-drain leaves the lock body.
    assert "stop_grace_period" in COMPOSE
