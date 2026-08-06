"""install.sh, run for real: the guard, the checksum gate, and the hand-off.

The script is executed as a script — no reimplementation of its logic here — against a release
served over HTTP. Two of its prerequisites are macOS-only (`uname -s` answering Darwin, and
`shasum`), so those are supplied as PATH shims; everything the script itself decides is its own.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parents[1] / "install.sh"

# The subject is the POSIX front door itself, executed by a real /bin/sh. Windows has its own
# door (install.ps1) and no sh to run this one with; the MSYS sh a git install brings along is
# not what a seller would be using either.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="install.sh is the POSIX front door")


def _executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def macos_shims(tmp_path):
    """A PATH where this machine looks like a Mac with the tools the script asks for."""
    binaries = tmp_path / "shims"
    binaries.mkdir()
    _executable(binaries / "uname", '#!/bin/sh\n[ "$1" = "-s" ] && echo Darwin || echo Darwin\n')
    if shutil.which("shasum") is None:
        _executable(binaries / "shasum", '#!/bin/sh\nshift 2\nexec sha256sum "$@"\n')
    return f"{binaries}{os.pathsep}{os.environ['PATH']}"


@pytest.fixture
def release(tmp_path):
    """A served release whose ./setup records that it ran, and with what."""
    served = tmp_path / "releases"
    served.mkdir()
    stage = tmp_path / "stage" / "selly-agent-1.2.3"
    stage.mkdir(parents=True)
    receipt = tmp_path / "setup-ran.txt"
    _executable(stage / "setup", f'#!/bin/sh\necho "setup ran: $*" > {receipt}\nexit 0\n')

    name = "selly-agent-1.2.3.tar.gz"
    with tarfile.open(served / name, "w:gz") as tar:
        tar.add(stage, arcname=stage.name)
    digest = hashlib.sha256((served / name).read_bytes()).hexdigest()
    (served / "SHA256SUMS").write_text(f"{digest}  {name}\n")

    handler = partial(SimpleHTTPRequestHandler, directory=str(served))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield served, f"http://127.0.0.1:{httpd.server_address[1]}", receipt
    finally:
        httpd.shutdown()
        httpd.server_close()


def run_install(*args, base_url=None, path=None):
    env = dict(os.environ)
    if base_url is not None:
        env["SELLY_INSTALL_BASE_URL"] = base_url
    else:
        env.pop("SELLY_INSTALL_BASE_URL", None)
    if path:
        env["PATH"] = path
    return subprocess.run(
        ["sh", str(INSTALL_SH), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


def test_without_release_hosting_it_says_so_and_installs_nothing() -> None:
    result = run_install()
    assert result.returncode == 1
    assert "isn't supported yet" in result.stderr
    assert "git clone" in result.stderr


def test_it_states_what_it_will_do_before_doing_it(macos_shims, release) -> None:
    _served, base, _receipt = release
    result = run_install(base_url=base, path=macos_shims)
    preamble = result.stdout.split("Fetching")[0]
    assert "SHA256SUMS" in preamble
    assert "temporary directory" in preamble
    assert "before writing" in preamble


def test_a_verified_release_is_unpacked_and_its_own_setup_takes_over(macos_shims, release) -> None:
    _served, base, receipt = release
    result = run_install("--yes", "--manual", base_url=base, path=macos_shims)
    assert result.returncode == 0, result.stderr
    assert receipt.read_text().strip() == "setup ran: --yes --manual"


def test_a_tampered_archive_is_refused_and_setup_never_runs(macos_shims, release) -> None:
    served, base, receipt = release
    archive = served / "selly-agent-1.2.3.tar.gz"
    archive.write_bytes(archive.read_bytes() + b"tampered")

    result = run_install(base_url=base, path=macos_shims)

    assert result.returncode == 1
    assert "does not match its published checksum" in result.stderr
    assert not receipt.exists()


def test_a_host_serving_nothing_fails_before_it_writes_anything(macos_shims) -> None:
    result = run_install(base_url="http://127.0.0.1:1/nowhere", path=macos_shims)
    assert result.returncode == 1
    assert "couldn't download" in result.stderr


def test_it_refuses_to_run_anywhere_but_macos(release) -> None:
    # No shims, so `uname -s` answers whatever this machine really is.
    _served, base, _receipt = release
    if sys.platform == "darwin":
        pytest.skip("this machine really is a Mac")
    result = run_install(base_url=base)
    assert result.returncode == 1
    assert "runs on macOS" in result.stderr


def test_dev_is_refused_because_the_tree_it_would_point_at_is_temporary(
    macos_shims, release
) -> None:
    _served, base, receipt = release
    result = run_install("--dev", base_url=base, path=macos_shims)
    assert result.returncode == 1
    assert "--dev needs a checkout" in result.stderr
    assert not receipt.exists()


def test_an_ambiguous_checksum_file_is_refused_rather_than_guessed(macos_shims, release) -> None:
    served, base, receipt = release
    sums = served / "SHA256SUMS"
    sums.write_text(sums.read_text() + f"{'a' * 64}  selly-agent-9.9.9.tar.gz\n")

    result = run_install(base_url=base, path=macos_shims)

    assert result.returncode == 1
    assert "more than one archive" in result.stderr
    assert not receipt.exists()
