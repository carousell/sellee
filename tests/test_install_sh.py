"""install.sh, run for real: the checksum gate and the hand-off.

The script is executed as a script — no reimplementation of its logic here — against a release
served over HTTP. The OS it thinks it is on is supplied as a `uname` shim, so both supported
platforms are exercised on whichever machine runs the suite; everything the script itself decides
is its own.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parents[1] / "install.sh"


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture(params=["Darwin", "Linux"])
def host_shims(request, tmp_path):
    """A PATH where this machine looks like each OS the script accepts, with the tools it asks
    for. Every test taking this fixture runs twice, so nothing may pass on one OS alone."""
    binaries = tmp_path / f"shims-{request.param}"
    binaries.mkdir()
    _executable(binaries / "uname", f"#!/bin/sh\necho {request.param}\n")
    if shutil.which("shasum") is None:
        _executable(binaries / "shasum", '#!/bin/sh\nshift 2\nexec sha256sum "$@"\n')
    return f"{binaries}{os.pathsep}{os.environ['PATH']}"


# What install.sh reaches for besides the shell's own builtins, plus the `sh` the test runner
# invokes it with. A PATH holding only these and a digest tool is how the sha256sum-only case
# below is made real rather than assumed.
_EXTERNAL_TOOLS = ("sh", "curl", "tar", "gzip", "mktemp", "awk", "wc", "grep", "find", "head", "rm")


@pytest.fixture
def coreutils_only_shims(tmp_path):
    """A Linux PATH holding sha256sum and no shasum at all — a stock Debian or Fedora.

    Built as a closed set rather than by prepending to the real PATH, because `shasum` cannot be
    hidden by prepending: whatever else is on PATH would still be found.
    """
    binaries = tmp_path / "shims-coreutils"
    binaries.mkdir()
    _executable(binaries / "uname", "#!/bin/sh\necho Linux\n")
    for tool in (*_EXTERNAL_TOOLS, "sha256sum"):
        found = shutil.which(tool)
        if found is None:
            pytest.skip(f"this machine has no {tool}")
        (binaries / tool).symlink_to(found)
    assert shutil.which("shasum", path=str(binaries)) is None
    return str(binaries)


@pytest.fixture
def release(tmp_path):
    """A served release whose ./setup records that it ran, and with what."""
    served = tmp_path / "releases"
    served.mkdir()
    stage = tmp_path / "stage" / "sellee-1.2.3"
    stage.mkdir(parents=True)
    receipt = tmp_path / "setup-ran.txt"
    _executable(stage / "setup", f'#!/bin/sh\necho "setup ran: $*" > {receipt}\nexit 0\n')

    name = "sellee-1.2.3.tar.gz"
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


_RELEASE_URL_LINE = re.compile(r"^RELEASE_URL=.*$", re.M)


def _sh(script, args, env):
    return subprocess.run(
        ["sh", str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


def run_install(*args, base_url=None, path=None):
    """Run install.sh, optionally against a locally served release.

    The script takes no base-URL override, so pointing it somewhere else means editing it — done
    here in a copy. Nothing below asserts anything about where it fetched from; the rewrite only
    gets the script past its own hardcoded URL so the checksum gate and the archive-name guard can
    be exercised for real.
    """
    env = dict(os.environ)
    if path:
        env["PATH"] = path
    if base_url is None:
        return _sh(INSTALL_SH, args, env)
    source, count = _RELEASE_URL_LINE.subn(
        f'RELEASE_URL="{base_url}"', INSTALL_SH.read_text(), count=1
    )
    assert count == 1, "install.sh no longer has exactly one RELEASE_URL assignment to rewrite"
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "install-under-test.sh"
        script.write_text(source)
        return _sh(script, args, env)


def test_it_states_what_it_will_do_before_doing_it(host_shims, release) -> None:
    _served, base, _receipt = release
    result = run_install(base_url=base, path=host_shims)
    preamble = result.stdout.split("Fetching")[0]
    assert "SHA256SUMS" in preamble
    assert "temporary directory" in preamble
    assert "before writing" in preamble


def test_a_verified_release_is_unpacked_and_its_own_setup_takes_over(host_shims, release) -> None:
    _served, base, receipt = release
    result = run_install("--yes", "--manual", base_url=base, path=host_shims)
    assert result.returncode == 0, result.stderr
    assert receipt.read_text().strip() == "setup ran: --yes --manual"


def test_a_tampered_archive_is_refused_and_setup_never_runs(host_shims, release) -> None:
    served, base, receipt = release
    archive = served / "sellee-1.2.3.tar.gz"
    archive.write_bytes(archive.read_bytes() + b"tampered")

    result = run_install(base_url=base, path=host_shims)

    assert result.returncode == 1
    assert "does not match its published checksum" in result.stderr
    assert not receipt.exists()


def test_a_host_serving_nothing_fails_before_it_writes_anything(host_shims) -> None:
    result = run_install(base_url="http://127.0.0.1:1/nowhere", path=host_shims)
    assert result.returncode == 1
    assert "couldn't download" in result.stderr


def test_it_refuses_an_os_it_has_no_installer_for(release, tmp_path) -> None:
    _served, base, _receipt = release
    binaries = tmp_path / "shims-other"
    binaries.mkdir()
    _executable(binaries / "uname", "#!/bin/sh\necho SunOS\n")

    result = run_install(base_url=base, path=f"{binaries}{os.pathsep}{os.environ['PATH']}")

    assert result.returncode == 1
    assert "runs on macOS and Linux" in result.stderr


def test_a_machine_with_only_sha256sum_still_verifies_the_archive(coreutils_only_shims, release):
    """`shasum` is a macOS tool; GNU coreutils ships `sha256sum` and no `shasum` at all. Demanding
    both would refuse a Linux machine that can check the download perfectly well."""
    _served, base, receipt = release
    result = run_install("--yes", base_url=base, path=coreutils_only_shims)
    assert result.returncode == 0, result.stderr
    assert receipt.read_text().strip() == "setup ran: --yes"


def test_a_machine_with_only_sha256sum_still_refuses_a_tampered_archive(
    coreutils_only_shims, release
):
    served, base, receipt = release
    archive = served / "sellee-1.2.3.tar.gz"
    archive.write_bytes(archive.read_bytes() + b"tampered")

    result = run_install(base_url=base, path=coreutils_only_shims)

    assert result.returncode == 1
    assert "does not match its published checksum" in result.stderr
    assert not receipt.exists()


def test_dev_is_refused_because_the_tree_it_would_point_at_is_temporary(
    host_shims, release
) -> None:
    _served, base, receipt = release
    result = run_install("--dev", base_url=base, path=host_shims)
    assert result.returncode == 1
    assert "--dev needs a checkout" in result.stderr
    assert not receipt.exists()


def test_an_ambiguous_checksum_file_is_refused_rather_than_guessed(host_shims, release) -> None:
    served, base, receipt = release
    sums = served / "SHA256SUMS"
    sums.write_text(sums.read_text() + f"{'a' * 64}  sellee-9.9.9.tar.gz\n")

    result = run_install(base_url=base, path=host_shims)

    assert result.returncode == 1
    assert "more than one archive" in result.stderr
    assert not receipt.exists()


# --- the archive name (SEC-2822) ----------------------------------------------------------------


def test_an_archive_name_carrying_a_path_is_refused_before_the_download(
    host_shims, release
) -> None:
    """The name comes out of a file the network handed us and is used as a `curl -o` path, and the
    awk filter's `.*` matches a slash — so a traversing name would write outside the temp dir
    before the digest gate ran at all."""
    served, base, receipt = release
    (served / "SHA256SUMS").write_text(f"{'a' * 64}  sellee-../../../pwned.tar.gz\n")

    result = run_install(base_url=base, path=host_shims)

    assert result.returncode == 1
    assert "an archive with a path in it" in result.stderr
    assert not receipt.exists()
