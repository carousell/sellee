"""Establishing the Python runtime: get uv, get the pinned interpreter, build the venv.

What this code runs on is never the user's own python3 — it is a standalone interpreter uv
provisions at a pinned version, with dependencies replayed from a lock file. Not depending on
system Python removes a whole class of install failure: too old, broken cert store, Store stub.

uv is fetched as a release archive and held to a digest recorded in the repo, deliberately not
installed by the vendor's script — that script edits shell startup files, and this installer
already owns one fenced block in one of them that its uninstall removes precisely. So uv lands
under our data root, is invoked by absolute path, and goes away when the data root does.

This module must stay stdlib-only: it is what makes the dependencies importable, so it cannot
use them.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from selly_agent import paths

log = logging.getLogger(__name__)

PIN_FILE = paths.PACKAGE_DATA_DIR / "uv-pin.txt"

_RELEASE_URL = "https://github.com/astral-sh/uv/releases/download/{version}/{asset}"

_FETCH_TIMEOUT_SEC = 300.0
_DOWNLOAD_CHUNK = 1 << 16
# Generous: a cold run downloads an interpreter and a wheel set.
_SYNC_TIMEOUT_SEC = 900.0
_PROBE_TIMEOUT_SEC = 60.0


class RuntimeSetupError(Exception):
    """The runtime could not be established, with a message meant for the person installing."""


# --- the pin ------------------------------------------------------------------------------


def read_pin(pin_file: Path | None = None) -> tuple:
    """The pinned uv version and the digest table, as (version, {triple: sha256})."""
    path = Path(pin_file) if pin_file is not None else PIN_FILE
    try:
        text = path.read_text()
    except OSError as exc:
        raise RuntimeSetupError(f"cannot read the uv pin at {path}: {exc}") from exc
    version = ""
    digests = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if fields[0] == "version" and len(fields) == 2:
            version = fields[1]
        elif fields[0] == "sha256" and len(fields) == 3:
            digests[fields[1]] = fields[2].lower()
    if not version or not digests:
        raise RuntimeSetupError(f"the uv pin at {path} names no version or no digests")
    return version, digests


def read_python_pin(tree: Path) -> str:
    """The interpreter version a tree pins, from its .python-version."""
    path = Path(tree) / ".python-version"
    try:
        pinned = path.read_text().strip()
    except OSError as exc:
        raise RuntimeSetupError(f"cannot read the interpreter pin at {path}: {exc}") from exc
    if not pinned:
        raise RuntimeSetupError(f"{path} is empty — it must name the interpreter version")
    return pinned


# --- platform → asset ---------------------------------------------------------------------

# uv names its release archives by Rust target triple.
_TRIPLES = {
    ("darwin", "arm64"): "aarch64-apple-darwin",
    ("darwin", "x86_64"): "x86_64-apple-darwin",
    ("linux", "arm64"): "aarch64-unknown-linux-gnu",
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("windows", "arm64"): "aarch64-pc-windows-msvc",
    ("windows", "x86_64"): "x86_64-pc-windows-msvc",
}

# Every spelling of those two architectures that a host might report for itself.
_ARCHITECTURES = {
    "aarch64": "arm64",
    "arm64": "arm64",
    "amd64": "x86_64",
    "x64": "x86_64",
    "x86_64": "x86_64",
}


def target_triple(system: str | None = None, machine: str | None = None) -> str:
    """The uv release triple for a host. Raises rather than guessing on something unmapped."""
    import platform as platform_module

    os_name = (system or platform_module.system()).lower()
    reported = (machine or platform_module.machine()).lower()
    triple = _TRIPLES.get((os_name, _ARCHITECTURES.get(reported, "")))
    if triple is None:
        raise RuntimeSetupError(
            f"no pinned uv build for {os_name}/{reported} — supported: "
            + ", ".join(sorted(f"{a}/{b}" for a, b in _TRIPLES))
        )
    return triple


def asset_name(triple: str) -> str:
    return f"uv-{triple}.zip" if "windows" in triple else f"uv-{triple}.tar.gz"


def download_url(version: str, asset: str) -> str:
    return _RELEASE_URL.format(version=version, asset=asset)


# --- subprocess ---------------------------------------------------------------------------


def _run(argv, *, cwd=None, timeout: float = _PROBE_TIMEOUT_SEC) -> tuple:
    try:
        completed = subprocess.run(
            [str(arg) for arg in argv],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]}: timed out after {timeout:.0f}s"
    except OSError as exc:
        # Absent, or present but not executable: both are answers about the runtime.
        return 127, "", f"{argv[0]}: {exc}"
    return completed.returncode, completed.stdout, completed.stderr


# --- acquiring uv -------------------------------------------------------------------------


def serves_pin(binary: Path, pinned: str) -> bool:
    """Whether a uv can offer a *final* build of the interpreter version we pin.

    Asked rather than inferred from uv's own version, because the two are only loosely related:
    uv refreshes its list of downloadable interpreters over the network, so an old uv usually
    knows about new releases — but one that cannot reach that list falls back to whatever was
    baked in when it was built, which for an old enough uv is a pre-release. The question that
    matters is what this uv can actually provide now, so that is the question.
    """
    if not Path(binary).exists():
        return False
    code, out, _ = _run([binary, "python", "list"])
    if code != 0:
        return False
    # A final release is `cpython-3.14.6-<platform>`; a pre-release puts a marker before the
    # dash (`cpython-3.14.0b1-<platform>`), so requiring digits-then-dash excludes it.
    pattern = re.compile(rf"^cpython-{re.escape(pinned)}(\.\d+)*-", re.MULTILINE)
    return bool(pattern.search(out))


def _fetch(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with (
            urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_SEC) as response,
            dest.open("wb") as out,
        ):
            shutil.copyfileobj(response, out, _DOWNLOAD_CHUNK)
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeSetupError(f"could not download {url}: {exc}") from exc


def _extract_binary(archive: Path, dest: Path) -> None:
    """Pull just the uv executable out of an archive, by name.

    Only the matching entry's *content* is used, never its path as a write destination — so a
    crafted member name has nothing to aim at, which beats sanitising an extract-everything call.
    """
    wanted = {"uv", "uv.exe"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            for name in bundle.namelist():
                if Path(name).name in wanted:
                    with bundle.open(name) as source, dest.open("wb") as out:
                        shutil.copyfileobj(source, out)
                    return
    else:
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle.getmembers():
                if member.isfile() and Path(member.name).name in wanted:
                    source = bundle.extractfile(member)
                    if source is None:
                        break
                    with source, dest.open("wb") as out:
                        shutil.copyfileobj(source, out)
                    return
    raise RuntimeSetupError(f"{archive.name} contains no uv executable")


def fetch_uv(*, pin_file: Path | None = None) -> Path:
    """Download the pinned uv, refuse it unless it matches our recorded digest, install it."""
    version, digests = read_pin(pin_file)
    triple = target_triple()
    expected = digests.get(triple)
    if not expected:
        raise RuntimeSetupError(
            f"no digest recorded for {triple} in {pin_file or PIN_FILE} — refusing to run an "
            "unverified binary"
        )
    asset = asset_name(triple)
    destination = paths.uv_path()
    with tempfile.TemporaryDirectory(prefix="selly-uv-") as scratch:
        archive = Path(scratch) / asset
        _fetch(download_url(version, asset), archive)
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeSetupError(
                f"{asset} does not match the digest recorded for uv {version} — refusing it "
                f"(expected {expected}, got {actual})"
            )
        staged = Path(scratch) / destination.name
        _extract_binary(archive, staged)
        staged.chmod(0o755)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Replace rather than write in place: a half-written binary must never be runnable.
        shutil.move(str(staged), str(destination))
    log.info("installed uv %s at %s", version, destination)
    return destination


def ensure_uv(tree: Path, *, pin_file: Path | None = None) -> Path:
    """The uv to use: whichever already-present one can serve the interpreter this tree pins,
    preferring the machine's own; otherwise our pinned copy, fetched if it is not here yet."""
    pinned = read_python_pin(tree)
    found = shutil.which("uv")
    if found and serves_pin(Path(found), pinned):
        return Path(found)
    ours = paths.uv_path()
    if serves_pin(ours, pinned):
        return ours
    return fetch_uv(pin_file=pin_file)


# --- the interpreter and the venv ---------------------------------------------------------


def ensure_interpreter(uv: Path, tree: Path) -> str:
    """Provision the pinned interpreter, and ask it whether it is a real release.

    An older uv answers a bare "3.14" with a pre-release, which is not something to discover
    after shipping sellers onto it.
    """
    pinned = read_python_pin(tree)
    code, _, err = _run([uv, "python", "install", pinned], timeout=_SYNC_TIMEOUT_SEC)
    if code != 0:
        raise RuntimeSetupError(f"uv could not install Python {pinned}: {err.strip()}")
    code, out, err = _run([uv, "python", "find", pinned])
    if code != 0:
        raise RuntimeSetupError(f"uv installed Python {pinned} but cannot find it: {err.strip()}")
    interpreter = out.strip()
    probe = "import sys; print(sys.version_info.releaselevel, sys.version.split()[0])"
    code, out, err = _run([interpreter, "-c", probe])
    if code != 0:
        raise RuntimeSetupError(f"{interpreter} does not run: {err.strip()}")
    level, found = (out.split() + ["", ""])[:2]
    if level != "final":
        raise RuntimeSetupError(
            f"uv provisioned Python {found} ({level}), not a release build — a newer uv is "
            f"needed to serve {pinned}"
        )
    return found


def sync(uv: Path, tree: Path, *, dev: bool = False) -> Path:
    """Build the tree's venv from its lock file, and answer the venv's interpreter.

    --locked replays the lock and fails rather than resolving something new, so what a seller
    installs is what was reviewed.
    """
    argv = [uv, "sync", "--locked"]
    if not dev:
        argv.append("--no-dev")
    code, _, err = _run(argv, cwd=tree, timeout=_SYNC_TIMEOUT_SEC)
    if code != 0:
        raise RuntimeSetupError(f"could not install dependencies into {tree}: {err.strip()}")
    interpreter = paths.venv_python(tree)
    if not interpreter.exists():
        raise RuntimeSetupError(f"dependency install left no interpreter at {interpreter}")
    return interpreter


def provision(tree: Path, *, dev: bool = False, pin_file: Path | None = None) -> Path:
    """Everything a tree needs to be runnable: uv, the pinned interpreter, its venv.

    A uv we borrowed from the machine that turns out not to deliver a final release is replaced
    by our own pinned copy rather than failing the install — the seller did not choose their uv,
    and we have one we know the answer for.
    """
    uv = ensure_uv(tree, pin_file=pin_file)
    try:
        ensure_interpreter(uv, tree)
    except RuntimeSetupError:
        if uv == paths.uv_path():
            raise
        log.info("%s could not provide the pinned interpreter; using our own uv", uv)
        uv = fetch_uv(pin_file=pin_file)
        ensure_interpreter(uv, tree)
    return sync(uv, tree, dev=dev)


# --- reporting ----------------------------------------------------------------------------


def describe(tree: Path) -> dict:
    """What is actually present for a tree — for a preflight check to report ground truth."""
    interpreter = paths.venv_python(tree)
    present = interpreter.exists()
    dependency_ok = False
    detail = ""
    if present:
        code, out, err = _run([interpreter, "-c", "import psutil; print(psutil.__version__)"])
        dependency_ok = code == 0
        detail = out.strip() if dependency_ok else err.strip().splitlines()[-1:][0] if err else ""
    return {
        "interpreter": str(interpreter),
        "present": present,
        "dependencies_importable": dependency_ok,
        "detail": detail,
        "running_in_venv": Path(sys.prefix) == paths.venv_dir(tree),
    }
