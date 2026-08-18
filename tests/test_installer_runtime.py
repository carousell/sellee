"""The runtime establishment layer: the pin, the platform table, and what gets refused.

Nothing here reaches the network. The download path is exercised by pointing the module at a
local file and by driving its digest check directly, which is the part worth pinning: a binary
that does not match the recorded digest must not end up installed.
"""

from __future__ import annotations

import hashlib
import tarfile
import zipfile
from pathlib import Path

import pytest

from sellee import paths
from sellee.installer import checks, preflight, runtime

# Captured before the autouse fixture that stubs provisioning for every other test replaces it —
# this module is where provision itself is under test.
_REAL_PROVISION = runtime.provision


def _pin(tmp_path: Path, version: str = "0.12.1", triple: str = "x86_64-unknown-linux-gnu") -> Path:
    path = tmp_path / "uv-pin.txt"
    path.write_text(f"# comment\n\nversion {version}\nsha256 {triple} " + ("ab" * 32) + "\n")
    return path


# --- the pin ------------------------------------------------------------------------------


def test_read_pin_parses_version_and_digests(tmp_path):
    version, digests = runtime.read_pin(_pin(tmp_path))
    assert version == "0.12.1"
    assert digests == {"x86_64-unknown-linux-gnu": "ab" * 32}


def test_read_pin_rejects_a_pin_with_no_digests(tmp_path):
    path = tmp_path / "uv-pin.txt"
    path.write_text("version 0.12.1\n")
    with pytest.raises(runtime.RuntimeSetupError):
        runtime.read_pin(path)


def test_read_pin_reports_a_missing_file(tmp_path):
    with pytest.raises(runtime.RuntimeSetupError):
        runtime.read_pin(tmp_path / "absent.txt")


def test_shipped_pin_covers_every_supported_platform():
    """The pin ships with the package, and every platform the triple table maps must have a
    digest — otherwise that host can reach a fetch it is then refused."""
    version, digests = runtime.read_pin()
    assert version
    for system, machine in [
        ("Darwin", "arm64"),
        ("Darwin", "x86_64"),
        ("Linux", "aarch64"),
        ("Linux", "x86_64"),
        ("Windows", "ARM64"),
        ("Windows", "AMD64"),
    ]:
        assert runtime.target_triple(system, machine) in digests


def test_read_python_pin(tmp_path):
    (tmp_path / ".python-version").write_text("3.14\n")
    assert runtime.read_python_pin(tmp_path) == "3.14"


def test_read_python_pin_rejects_an_empty_file(tmp_path):
    (tmp_path / ".python-version").write_text("  \n")
    with pytest.raises(runtime.RuntimeSetupError):
        runtime.read_python_pin(tmp_path)


# --- platform → asset ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "aarch64-apple-darwin"),
        ("Darwin", "aarch64", "aarch64-apple-darwin"),
        ("Darwin", "x86_64", "x86_64-apple-darwin"),
        ("Linux", "x86_64", "x86_64-unknown-linux-gnu"),
        ("Linux", "aarch64", "aarch64-unknown-linux-gnu"),
        ("Linux", "arm64", "aarch64-unknown-linux-gnu"),
        ("Windows", "AMD64", "x86_64-pc-windows-msvc"),
        ("Windows", "ARM64", "aarch64-pc-windows-msvc"),
    ],
)
def test_target_triple(system, machine, expected):
    assert runtime.target_triple(system, machine) == expected


def test_target_triple_refuses_an_unmapped_host():
    with pytest.raises(runtime.RuntimeSetupError) as excinfo:
        runtime.target_triple("Solaris", "sparc")
    assert "no pinned uv build" in str(excinfo.value)


def test_asset_name_picks_zip_only_for_windows():
    assert runtime.asset_name("x86_64-pc-windows-msvc").endswith(".zip")
    assert runtime.asset_name("aarch64-apple-darwin").endswith(".tar.gz")


def test_download_url_needs_no_api_call():
    url = runtime.download_url("0.12.1", "uv-aarch64-apple-darwin.tar.gz")
    assert url == (
        "https://github.com/astral-sh/uv/releases/download/0.12.1/uv-aarch64-apple-darwin.tar.gz"
    )


# --- is a given uv good enough? ------------------------------------------------------------


def _fake_uv(tmp_path: Path, listing: str) -> Path:
    """A stand-in uv whose `python list` prints whatever a test wants it to."""
    fake = tmp_path / "uv"
    fake.write_text(f"#!/bin/sh\ncat <<'EOF'\n{listing}\nEOF\n")
    fake.chmod(0o755)
    return fake


def test_serves_pin_is_false_for_a_missing_binary(tmp_path):
    assert runtime.serves_pin(tmp_path / "uv", "3.14") is False


def test_serves_pin_accepts_a_uv_offering_a_final_release(tmp_path):
    fake = _fake_uv(tmp_path, "cpython-3.14.6-macos-aarch64-none    <download available>")
    assert runtime.serves_pin(fake, "3.14") is True


def test_serves_pin_rejects_a_uv_offering_only_a_pre_release(tmp_path):
    """The case the whole probe exists for: an old uv that cannot refresh its list of
    interpreters offers a beta, and shipping a seller onto one is not acceptable."""
    fake = _fake_uv(tmp_path, "cpython-3.14.0b1-macos-aarch64-none    <download available>")
    assert runtime.serves_pin(fake, "3.14") is False


def test_serves_pin_rejects_a_uv_that_knows_nothing_about_the_pin(tmp_path):
    fake = _fake_uv(tmp_path, "cpython-3.13.14-macos-aarch64-none    <download available>")
    assert runtime.serves_pin(fake, "3.14") is False


def test_serves_pin_is_not_fooled_by_a_longer_version(tmp_path):
    """3.1 must not be satisfied by 3.14 — the boundary has to be a real separator."""
    fake = _fake_uv(tmp_path, "cpython-3.14.6-macos-aarch64-none    <download available>")
    assert runtime.serves_pin(fake, "3.1") is False


def test_serves_pin_accepts_a_fully_qualified_pin(tmp_path):
    fake = _fake_uv(tmp_path, "cpython-3.14.6-macos-aarch64-none    <download available>")
    assert runtime.serves_pin(fake, "3.14.6") is True


# --- extraction and the digest gate --------------------------------------------------------


def _tar_with(tmp_path: Path, entries: dict) -> Path:
    archive = tmp_path / "uv-x86_64-unknown-linux-gnu.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for name, content in entries.items():
            payload = tmp_path / Path(name).name
            payload.write_bytes(content)
            bundle.add(payload, arcname=name)
    return archive


def test_extract_binary_takes_the_uv_entry_from_a_nested_tar(tmp_path):
    archive = _tar_with(
        tmp_path, {"uv-x86_64-unknown-linux-gnu/uv": b"BINARY", "README.md": b"docs"}
    )
    dest = tmp_path / "out" / "uv"
    runtime._extract_binary(archive, dest)
    assert dest.read_bytes() == b"BINARY"


def test_extract_binary_ignores_a_traversing_member_name(tmp_path):
    archive = _tar_with(tmp_path, {"../../../../tmp/evil/uv": b"BINARY"})
    dest = tmp_path / "out" / "uv"
    runtime._extract_binary(archive, dest)
    assert dest.read_bytes() == b"BINARY"
    assert not Path("/tmp/evil").exists()


def test_extract_binary_handles_a_windows_zip(tmp_path):
    archive = tmp_path / "uv-x86_64-pc-windows-msvc.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("uv.exe", b"WINBINARY")
    dest = tmp_path / "uv.exe"
    runtime._extract_binary(archive, dest)
    assert dest.read_bytes() == b"WINBINARY"


def test_extract_binary_refuses_an_archive_without_uv(tmp_path):
    archive = _tar_with(tmp_path, {"uv-x86_64-unknown-linux-gnu/LICENSE": b"text"})
    with pytest.raises(runtime.RuntimeSetupError) as excinfo:
        runtime._extract_binary(archive, tmp_path / "uv")
    assert "no uv executable" in str(excinfo.value)


def test_fetch_uv_refuses_a_digest_mismatch(tmp_path, monkeypatch, xdg_tmp):
    """The whole point of recording digests: a mismatched download must not become the binary
    we then execute."""
    payload = b"NOT-THE-PINNED-BINARY"
    archive_source = _tar_with(tmp_path, {"uv-x86_64-unknown-linux-gnu/uv": payload})

    def fake_fetch(url, dest):
        dest.write_bytes(archive_source.read_bytes())

    monkeypatch.setattr(runtime, "_fetch", fake_fetch)
    monkeypatch.setattr(runtime, "target_triple", lambda *a, **k: "x86_64-unknown-linux-gnu")

    with pytest.raises(runtime.RuntimeSetupError) as excinfo:
        runtime.fetch_uv(pin_file=_pin(tmp_path))
    assert "does not match the digest" in str(excinfo.value)
    assert not paths.uv_path().exists()


def test_fetch_uv_installs_an_executable_on_a_digest_match(tmp_path, monkeypatch, xdg_tmp):
    payload = b"THE-PINNED-BINARY"
    archive_source = _tar_with(tmp_path, {"uv-x86_64-unknown-linux-gnu/uv": payload})
    digest = hashlib.sha256(archive_source.read_bytes()).hexdigest()

    pin = tmp_path / "uv-pin.txt"
    pin.write_text(f"version 0.12.1\nsha256 x86_64-unknown-linux-gnu {digest}\n")

    def fake_fetch(url, dest):
        dest.write_bytes(archive_source.read_bytes())

    monkeypatch.setattr(runtime, "_fetch", fake_fetch)
    monkeypatch.setattr(runtime, "target_triple", lambda *a, **k: "x86_64-unknown-linux-gnu")

    installed = runtime.fetch_uv(pin_file=pin)
    assert installed == paths.uv_path()
    assert installed.read_bytes() == payload
    assert installed.stat().st_mode & 0o111, "the fetched uv must be executable"


def test_fetch_uv_refuses_a_host_with_no_recorded_digest(tmp_path, monkeypatch, xdg_tmp):
    monkeypatch.setattr(runtime, "target_triple", lambda *a, **k: "s390x-unknown-linux-gnu")
    with pytest.raises(runtime.RuntimeSetupError) as excinfo:
        runtime.fetch_uv(pin_file=_pin(tmp_path))
    assert "no digest recorded" in str(excinfo.value)


# --- choosing which uv to use --------------------------------------------------------------

_FINAL = "cpython-3.14.6-macos-aarch64-none    <download available>"
_PRERELEASE = "cpython-3.14.0b1-macos-aarch64-none    <download available>"


@pytest.fixture
def pinned_tree(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / ".python-version").write_text("3.14\n")
    return tree


def test_ensure_uv_uses_the_machines_own_uv_when_it_can_serve_the_pin(
    tmp_path, pinned_tree, monkeypatch, xdg_tmp
):
    """A capable uv is reused whatever its version — installing a second copy of a tool the
    machine already has is exactly what this avoids."""
    existing = _fake_uv(tmp_path, _FINAL)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: str(existing))

    def refuse(**kwargs):
        raise AssertionError("should not fetch when the machine's uv can serve the pin")

    monkeypatch.setattr(runtime, "fetch_uv", refuse)
    assert runtime.ensure_uv(pinned_tree, pin_file=_pin(tmp_path)) == existing


def test_ensure_uv_falls_back_when_the_machines_uv_offers_only_a_pre_release(
    tmp_path, pinned_tree, monkeypatch, xdg_tmp
):
    stale = _fake_uv(tmp_path, _PRERELEASE)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: str(stale))
    monkeypatch.setattr(runtime, "fetch_uv", lambda **kwargs: Path("/fetched/uv"))
    assert runtime.ensure_uv(pinned_tree, pin_file=_pin(tmp_path)) == Path("/fetched/uv")


def test_ensure_uv_reuses_our_own_previously_fetched_copy(
    tmp_path, pinned_tree, monkeypatch, xdg_tmp
):
    monkeypatch.setattr(runtime.shutil, "which", lambda name: None)
    ours = paths.uv_path()
    ours.parent.mkdir(parents=True, exist_ok=True)
    ours.write_text(f"#!/bin/sh\ncat <<'EOF'\n{_FINAL}\nEOF\n")
    ours.chmod(0o755)

    def refuse(**kwargs):
        raise AssertionError("should not re-fetch a uv we already installed")

    monkeypatch.setattr(runtime, "fetch_uv", refuse)
    assert runtime.ensure_uv(pinned_tree, pin_file=_pin(tmp_path)) == ours


def test_provision_replaces_a_borrowed_uv_that_cannot_deliver_a_final_release(
    tmp_path, pinned_tree, monkeypatch, xdg_tmp
):
    """The probe can be satisfied and the install still come up short. Rather than failing, our
    own pinned uv takes over — the seller did not choose the uv on their machine."""
    borrowed = _fake_uv(tmp_path, _FINAL)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: str(borrowed))
    ours = tmp_path / "ours-uv"
    monkeypatch.setattr(runtime, "fetch_uv", lambda **kwargs: ours)

    attempts = []

    def interpreter(uv, tree):
        attempts.append(uv)
        if uv == borrowed:
            raise runtime.RuntimeSetupError("provisioned a pre-release")
        return "3.14.6"

    monkeypatch.setattr(runtime, "ensure_interpreter", interpreter)
    monkeypatch.setattr(runtime, "sync", lambda uv, tree, dev=False: Path("/venv/python"))

    _REAL_PROVISION(pinned_tree, pin_file=_pin(tmp_path))
    assert attempts == [borrowed, ours]


# --- reporting ----------------------------------------------------------------------------


def test_describe_reports_an_absent_venv(tmp_path):
    report = runtime.describe(tmp_path)
    assert report["present"] is False
    assert report["dependencies_importable"] is False
    assert report["interpreter"] == str(paths.venv_python(tmp_path))


def test_describe_knows_whether_this_process_runs_in_the_tree_venv(tmp_path):
    assert runtime.describe(tmp_path)["running_in_venv"] is False


# --- the preflight gate --------------------------------------------------------------------


def test_the_gate_fails_when_a_tree_has_no_dependency_environment(tmp_path):
    result = preflight.check_runtime(tmp_path)
    assert result.status == checks.FAIL
    assert "no dependency environment" in result.detail
    assert "./setup" in result.fix


def test_the_gate_fails_when_the_interpreter_cannot_run(tmp_path):
    """A present-but-unusable venv — bad permissions, a truncated copy — is a different failure
    from an absent one, and asking by importing is what tells them apart."""
    interpreter = paths.venv_python(tmp_path)
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("not a binary\n")
    interpreter.chmod(0o644)
    result = preflight.check_runtime(tmp_path)
    assert result.status == checks.FAIL
    assert "not importable" in result.detail


def test_the_gate_passes_on_a_real_provisioned_tree():
    """This very tree: the suite is running on the interpreter it describes."""
    result = preflight.check_runtime(Path(__file__).resolve().parents[1])
    assert result.status == checks.OK


def test_describe_reports_a_working_dependency_on_this_tree():
    report = runtime.describe(Path(__file__).resolve().parents[1])
    assert report["present"] is True
    assert report["dependencies_importable"] is True
    assert report["running_in_venv"] is True
