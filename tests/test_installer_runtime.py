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

from selly_agent import paths
from selly_agent.installer import checks, preflight, runtime


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


# --- version comparison -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("uv 0.12.1 (abc123 2026-01-01)\n", "0.12.1"),
        ("uv 0.7.12\n", "0.7.12"),
        ("", ""),
        ("something else\n", ""),
    ],
)
def test_parse_uv_version(output, expected):
    assert runtime.parse_uv_version(output) == expected


def test_version_ordering_gates_the_floor():
    assert runtime.version_key("0.12.1") >= runtime.version_key("0.12.1")
    assert runtime.version_key("0.13.0") > runtime.version_key("0.12.1")
    # The sandbox-era uv, which serves only a 3.14 pre-release, must read as below the floor.
    assert runtime.version_key("0.7.12") < runtime.version_key("0.12.1")
    # Unparseable sorts lowest, so a strange build is replaced rather than trusted.
    assert runtime.version_key("garbage") < runtime.version_key("0.12.1")


def test_usable_uv_is_false_for_a_missing_binary(tmp_path):
    assert runtime.usable_uv(tmp_path / "uv", "0.12.1") is False


def test_usable_uv_rejects_a_too_old_binary(tmp_path):
    fake = tmp_path / "uv"
    fake.write_text("#!/bin/sh\necho 'uv 0.7.12'\n")
    fake.chmod(0o755)
    assert runtime.usable_uv(fake, "0.12.1") is False


def test_usable_uv_accepts_a_new_enough_binary(tmp_path):
    fake = tmp_path / "uv"
    fake.write_text("#!/bin/sh\necho 'uv 0.12.4 (abc 2026-02-02)'\n")
    fake.chmod(0o755)
    assert runtime.usable_uv(fake, "0.12.1") is True


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
    """Only the matching entry's *content* is used; its path is never a write destination, so a
    crafted name has nothing to aim at."""
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


def test_ensure_uv_prefers_a_new_enough_uv_already_on_path(tmp_path, monkeypatch, xdg_tmp):
    existing = tmp_path / "uv"
    existing.write_text("#!/bin/sh\necho 'uv 0.12.9'\n")
    existing.chmod(0o755)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: str(existing))

    def refuse(**kwargs):
        raise AssertionError("should not fetch when a usable uv is already installed")

    monkeypatch.setattr(runtime, "fetch_uv", refuse)
    assert runtime.ensure_uv(pin_file=_pin(tmp_path)) == existing


def test_ensure_uv_ignores_a_too_old_uv_on_path(tmp_path, monkeypatch, xdg_tmp):
    """An old uv is worse than no uv: it resolves the interpreter pin to a pre-release."""
    stale = tmp_path / "uv"
    stale.write_text("#!/bin/sh\necho 'uv 0.7.12'\n")
    stale.chmod(0o755)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: str(stale))
    monkeypatch.setattr(runtime, "fetch_uv", lambda **kwargs: Path("/fetched/uv"))
    assert runtime.ensure_uv(pin_file=_pin(tmp_path)) == Path("/fetched/uv")


def test_ensure_uv_reuses_our_own_previously_fetched_copy(tmp_path, monkeypatch, xdg_tmp):
    monkeypatch.setattr(runtime.shutil, "which", lambda name: None)
    ours = paths.uv_path()
    ours.parent.mkdir(parents=True, exist_ok=True)
    ours.write_text("#!/bin/sh\necho 'uv 0.12.1'\n")
    ours.chmod(0o755)

    def refuse(**kwargs):
        raise AssertionError("should not re-fetch a uv we already installed")

    monkeypatch.setattr(runtime, "fetch_uv", refuse)
    assert runtime.ensure_uv(pin_file=_pin(tmp_path)) == ours


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
