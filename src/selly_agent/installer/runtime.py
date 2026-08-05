"""Establishing the Python runtime: get uv, get the pinned interpreter, build the venv.

This is the one module that answers "what does this code run on". The answer is never the
user's own python3 — it is a standalone interpreter uv provisions at a version we pin, with a
dependency set replayed from a lock file. That removes an entire class of install failure
(system Python too old, built against a broken cert store, or a Store stub) by not depending
on system Python at all.

uv itself is fetched as a release archive and checked against a digest recorded in the repo.
Deliberately not the vendor's install script: that script edits shell startup files, and this
installer already owns exactly one fenced block in exactly one of those files, which its own
uninstall removes. A second writer we do not control would leave residue nothing can clean
up. So uv lands under our data root, is invoked by absolute path, and disappears when the
data root does.

Every command here is a subprocess against that binary — this module stays stdlib-only, since
it is what makes the dependencies importable in the first place.
"""

from __future__ import annotations

import hashlib
import logging
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

# Rust target triples, which is how uv names its release archives. Kept as data so a new
# platform is one row plus a digest, not a new branch.
_TRIPLES = {
    ("darwin", "arm64"): "aarch64-apple-darwin",
    ("darwin", "x86_64"): "x86_64-apple-darwin",
    ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("windows", "arm64"): "aarch64-pc-windows-msvc",
    ("windows", "x86_64"): "x86_64-pc-windows-msvc",
}

_MACHINE_ALIASES = {
    "amd64": "x86_64",
    "x64": "x86_64",
    "arm64": "arm64",
    "aarch64": "aarch64",
}


def target_triple(system: str | None = None, machine: str | None = None) -> str:
    """The uv release triple for a host. Raises rather than guessing on something unmapped."""
    import platform as platform_module

    os_name = (system or platform_module.system()).lower()
    arch = (machine or platform_module.machine()).lower()
    arch = _MACHINE_ALIASES.get(arch, arch)
    # macOS and Windows name the same 64-bit ARM differently; normalise onto each table key.
    if os_name == "darwin" and arch == "aarch64":
        arch = "arm64"
    elif os_name == "windows" and arch == "aarch64":
        arch = "arm64"
    elif os_name == "linux" and arch == "arm64":
        arch = "aarch64"
    triple = _TRIPLES.get((os_name, arch))
    if triple is None:
        raise RuntimeSetupError(
            f"no pinned uv build for {os_name}/{arch} — supported: "
            + ", ".join(sorted(f"{a}/{b}" for a, b in _TRIPLES))
        )
    return triple


def asset_name(triple: str) -> str:
    return f"uv-{triple}.zip" if "windows" in triple else f"uv-{triple}.tar.gz"


def download_url(version: str, asset: str) -> str:
    return _RELEASE_URL.format(version=version, asset=asset)


# --- version comparison -------------------------------------------------------------------


def version_key(text: str) -> tuple:
    """A sortable key for a uv version string. Unparseable reads as oldest, so an odd build
    string is refused in favour of our own pinned copy rather than trusted."""
    parts = []
    for chunk in (text or "").strip().split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        parts.append(int(digits) if digits else 0)
    return tuple((parts + [0, 0, 0])[:3])


def parse_uv_version(output: str) -> str:
    """The version out of `uv --version` ("uv 0.12.1 (abcdef 2026-01-01)")."""
    fields = (output or "").split()
    return fields[1] if len(fields) >= 2 and fields[0] == "uv" else ""


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
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]}: timed out after {timeout:.0f}s"
    return completed.returncode, completed.stdout, completed.stderr


def uv_version(binary: Path) -> str:
    code, out, _ = _run([binary, "--version"])
    return parse_uv_version(out) if code == 0 else ""


# --- acquiring uv -------------------------------------------------------------------------


def usable_uv(binary: Path, minimum: str) -> bool:
    """Whether a uv binary exists and is new enough to know the interpreter we pin."""
    if not Path(binary).exists():
        return False
    found = uv_version(Path(binary))
    return bool(found) and version_key(found) >= version_key(minimum)


def _fetch(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    try:
        with (
            urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_SEC) as response,
            dest.open("wb") as out,
        ):
            while True:
                chunk = response.read(_DOWNLOAD_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                out.write(chunk)
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeSetupError(f"could not download {url}: {exc}") from exc
    dest.with_suffix(dest.suffix + ".sha256").write_text(digest.hexdigest())


def _extract_binary(archive: Path, dest: Path) -> None:
    """Pull just the uv executable out of an archive, by name.

    Only the *content* of the matching entry is used — never its path as a write destination —
    so a crafted archive has nothing to aim at. That is stronger than sanitising an
    extract-everything call, and we want exactly one file.
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


def ensure_uv(*, pin_file: Path | None = None) -> Path:
    """The uv to use: one already on PATH if it is new enough, else our own pinned copy.

    Preferring an existing uv keeps us from installing a second copy of a tool the machine
    already has — but only when it can actually serve the interpreter we pin.
    """
    version, _ = read_pin(pin_file)
    found = shutil.which("uv")
    if found and usable_uv(Path(found), version):
        return Path(found)
    ours = paths.uv_path()
    if usable_uv(ours, version):
        return ours
    return fetch_uv(pin_file=pin_file)


# --- the interpreter and the venv ---------------------------------------------------------


def ensure_interpreter(uv: Path, tree: Path) -> str:
    """Provision the pinned interpreter and confirm it is a real release.

    A pre-release would satisfy a bare "3.14" request on an older uv, and shipping sellers onto
    a beta interpreter is not a thing to discover later, so this asks the interpreter itself.
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

    --locked, so this replays the lock and fails rather than quietly resolving something new:
    what a seller installs is what was reviewed and tested.
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
    """Everything a tree needs to be runnable: uv, the pinned interpreter, its venv."""
    uv = ensure_uv(pin_file=pin_file)
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
