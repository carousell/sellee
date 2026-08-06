"""`selly-agent update` — fetch, verify, swap, restart, check, and roll back if it went wrong.

Deliberately dumb, in the way that makes it trustworthy: it never reasons about what changed
between versions. It downloads an archive, refuses it unless its digest matches the published
one, unpacks it beside the running version, and moves a symlink. Nothing is mutated in place, so
the previous version is still sitting there intact if the new one does not come up.

Discovery is sums-first and needs no API: the checksum file names the release, so one fetch tells
us both the version on offer and the digest to hold the download to. That keeps this and the curl
bootstrap on one mechanism, and keeps a daemon that polls for updates from spending an
unauthenticated API quota to do it.

The schema is the reason rollback is more than a symlink flip. A new version migrates selly.db at
startup, and old code cannot read a migrated database — so the flip is paired with restoring the
snapshot the migration itself took, and only when a migration actually ran.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from selly_agent import __version__, config, heartbeat, paths, supervisor
from selly_agent.installer import checks, materialize, preflight, runtime

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://github.com/carousell/selly-agent/releases/latest/download"

_SUMS_NAME = "SHA256SUMS"
_ASSET_RE = re.compile(r"^selly-agent-(?P<version>.+)\.tar\.gz$")
# A version is a dotted run of numbers, optionally followed by a pre-release marker.
_VERSION_RE = re.compile(r"^v?(?P<release>\d+(?:\.\d+)*)(?P<pre>.*)$")
_VERSION_PARTS = 4

# Hosts allowed to serve a release over plain HTTP. The digest check is self-consistent — the
# checksums come from the same origin as the archive — so an unencrypted channel means whoever
# controls it chooses what code runs here. Loopback is exempt because there is no network to
# intercept, and that is what the staged-release tests serve from.
_PLAINTEXT_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_FETCH_TIMEOUT_SEC = 300.0
_DOWNLOAD_CHUNK = 1 << 16

# Exit code for `--check` when a newer version exists, so a script can branch on it without
# parsing our prose. 0 still means "nothing to do".
UPDATE_AVAILABLE_EXIT = 10


class UpdateError(Exception):
    """Anything that stops an update, with a message meant for the person who ran it."""


@dataclass(frozen=True)
class Release:
    version: str
    filename: str
    digest: str


# --- pure ---------------------------------------------------------------------------------------


def version_key(text: str) -> tuple:
    """A sortable key for a version string, where any pre-release sorts below its own release.

    The leading dotted-number run is the version; whatever follows it (`.dev0`, `-rc1`, `b2`) is
    a pre-release marker, which is why its presence *lowers* the key — `1.2.3-rc1` must not read
    as equal to `1.2.3`, or an install sitting on the candidate would never see the release.

    Deliberately forgiving about anything past that: the only question ever asked here is whether
    the published version is newer than this one, and refusing to answer because a tag had an
    unexpected shape would strand an install on an old version indefinitely.
    """
    found = _VERSION_RE.match(text.strip())
    if not found:
        return (0, 0, 0, 0, 0)
    numbers = tuple(int(part) for part in found.group("release").split("."))
    padded = (numbers + (0, 0, 0, 0))[:_VERSION_PARTS]
    return padded + (0 if found.group("pre").strip() else 1,)


def is_newer(candidate: str, current: str) -> bool:
    return version_key(candidate) > version_key(current)


def parse_sums(text: str) -> dict:
    """The `<digest>  <filename>` lines of a checksum file, as {filename: digest}."""
    entries = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, name = parts[0], parts[1].lstrip("*")
        if len(digest) == 64 and all(c in "0123456789abcdefABCDEF" for c in digest):
            entries[name] = digest.lower()
    return entries


def release_from_sums(text: str) -> Release:
    """The release a checksum file describes. The asset's own name carries the version, which is
    why no separate manifest or API call is needed to discover it."""
    candidates = []
    for name, digest in parse_sums(text).items():
        found = _ASSET_RE.match(name)
        if found:
            candidates.append(Release(version=found.group("version"), filename=name, digest=digest))
    if not candidates:
        raise UpdateError(f"no selly-agent archive listed in {_SUMS_NAME}")
    if len(candidates) > 1:
        # Several releases in one checksum file: take the highest rather than the first, so the
        # answer does not depend on line order.
        candidates.sort(key=lambda release: version_key(release.version))
    return candidates[-1]


def safe_members(archive: tarfile.TarFile, dest: Path):
    """Every member of an archive, refusing any that would write outside the destination.

    The digest already establishes that the archive is the one that was published, so this is
    not the primary defence — but an extraction that can be talked into writing to an absolute
    path is worth making structurally impossible rather than merely unlikely.
    """
    dest = dest.resolve()
    for member in archive.getmembers():
        # Absolute, drive-lettered and UNC names are refused by shape, not just by containment:
        # joining an absolute segment discards the base entirely, and what that yields depends
        # on the host's path rules — a property this explicit check does not lean on.
        raw = member.name.replace("\\", "/")
        if raw.startswith("/") or ":" in raw.split("/", 1)[0]:
            raise UpdateError(f"refusing archive: {member.name!r} is not a relative path")
        target = (dest / member.name).resolve()
        if target != dest and dest not in target.parents:
            raise UpdateError(f"refusing archive: {member.name!r} would write outside {dest}")
        if member.issym() or member.islnk():
            link_target = (target.parent / member.linkname).resolve()
            if link_target != dest and dest not in link_target.parents:
                raise UpdateError(f"refusing archive: {member.name!r} links outside {dest}")
        yield member


# --- the network ----------------------------------------------------------------------------------


def _fetch(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_SEC) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError(f"could not reach {url}: {exc}") from exc


def discover(base_url: str) -> Release:
    """Which release is on offer, read out of its own checksum file."""
    return release_from_sums(_fetch(f"{base_url.rstrip('/')}/{_SUMS_NAME}").decode("utf-8"))


def download(base_url: str, release: Release, into: Path) -> Path:
    """Download the archive and hold it to the published digest.

    A mismatch leaves the file where it is rather than deleting it: it is evidence, and the next
    attempt overwrites it anyway, so nothing accumulates.
    """
    into.mkdir(parents=True, exist_ok=True)
    dest = into / release.filename
    url = f"{base_url.rstrip('/')}/{release.filename}"
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
        raise UpdateError(f"could not download {url}: {exc}") from exc
    if digest.hexdigest() != release.digest:
        raise UpdateError(
            f"{release.filename} does not match its published checksum — refusing it "
            f"(the download is at {dest} if you want to look)"
        )
    return dest


def unpack(archive: Path, into: Path) -> Path:
    """Extract an archive and answer the tree root inside it."""
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True)
    # `filter="data"` is the interpreter's own hardening, and the default from 3.14. Passed only
    # where it exists — the 3.9 floor predates it, which is why safe_members does the same job
    # by hand rather than relying on it.
    hardening = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(into, members=safe_members(tar, into), **hardening)
    entries = [child for child in into.iterdir() if child.is_dir()]
    if len(entries) == 1 and (entries[0] / "src").is_dir():
        return entries[0]
    return into


# --- database snapshots -----------------------------------------------------------------------


def latest_snapshot():
    """The newest pre-migration snapshot of selly.db, or None when there is none.

    These are written by the migration runner, and only when a migration was actually pending —
    which is exactly what makes "did the new version migrate?" answerable after the fact.
    """
    backups = paths.backups_dir()
    if not backups.is_dir():
        return None
    found = sorted(backups.glob("selly-*-pre-*.db"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


_RESTORE_ATTEMPTS = 5
_RESTORE_BACKOFF_SEC = 0.5


def _retry_permission_denied(operation):
    """Run `operation`, retrying briefly on PermissionError.

    On Windows an antivirus scanner holds a just-closed file for a moment; a rollback that dies
    on that moment leaves the install half-restored, which is the one state this whole path
    exists to prevent.
    """
    for attempt in range(_RESTORE_ATTEMPTS):
        try:
            return operation()
        except PermissionError:
            if attempt == _RESTORE_ATTEMPTS - 1:
                raise
            time.sleep(_RESTORE_BACKOFF_SEC * (attempt + 1))


def restore_snapshot(snapshot: Path) -> None:
    """Put a snapshot back as selly.db. The daemon must be stopped: this replaces the file it
    would otherwise be holding open."""
    _retry_permission_denied(lambda: shutil.copy2(snapshot, paths.selly_db()))
    # A WAL alongside a restored database describes the *replaced* one, and replaying it would
    # undo the restore. The snapshot is a complete, checkpointed copy, so they can go.
    for suffix in ("-wal", "-shm"):
        sidecar = paths.selly_db().with_name(paths.selly_db().name + suffix)
        if sidecar.exists():
            _retry_permission_denied(sidecar.unlink)


# --- the daemon around the swap -------------------------------------------------------------------


def _stop_daemon(platform=None) -> bool:
    """Stop the daemon and confirm it is gone, answering whether it was running. Its state before
    the update decides whether it should be running after.

    Confirmed rather than requested, because everything after this replaces files the daemon has
    open — and on Windows an open file cannot be replaced at all, so a swap racing a live daemon
    fails partway instead of being merely untidy.
    """
    status = supervisor.gather_status(platform=platform)
    if not supervisor.shutdown(label=status.label, platform=platform):
        raise UpdateError(
            f"the background worker (pid {supervisor.daemon_pid()}) did not stop when asked, and "
            "updating would replace files it still has open — nothing has been changed"
        )
    return status.registered


def _start_daemon(mode: str, *, platform=None) -> bool:
    """Re-register the job and start it, then wait for it to say it is alive.

    `daemon install` runs again rather than just `start`, because the plist pins the interpreter's
    real path and the launcher's path: an update that only flipped the symlink would leave a job
    definition describing the version it replaced.
    """
    import time

    started_after = time.time()
    supervisor.install(mode=mode, platform=platform)
    supervisor.start(platform=platform)
    return heartbeat.wait_fresh(paths.heartbeat_path(), newer_than=started_after, timeout_sec=60.0)


# --- the daemon's own look ----------------------------------------------------------------------

UPDATE_NOTICE = (
    "selly-agent {version} is out (you're on {current}). Update when it suits you: "
    "`selly-agent update`."
)


def update_probe(*, store, bus, config_obj, seen: set) -> None:
    """Tell the seller once when a new version appears. Never installs anything.

    An update replaces the code a running daemon is executing, so it stays something a person
    starts. Dev installs are skipped entirely: `current` points at a working tree there, every
    real release outranks a .dev version, and the notice would be permanent noise.
    """
    if materialize.is_dev_install():
        return
    base = config_obj.update_base_url or DEFAULT_BASE_URL
    try:
        release = discover(base)
    except UpdateError as exc:
        log.debug("update check failed: %s", exc)
        return
    if not is_newer(release.version, __version__) or release.version in seen:
        return
    seen.add(release.version)
    store.queue_notice(UPDATE_NOTICE.format(version=release.version, current=__version__))
    bus.publish("update.available", {"version": release.version, "current": __version__})


# --- the flow ---------------------------------------------------------------------------------


def _base_url(args, cfg) -> str:
    """Where to fetch a release from, having established the channel can be trusted.

    Everything downloaded here is executed, and the checksum file travels the same channel as
    the archive it vouches for — so an attacker who can rewrite the channel rewrites both. That
    makes transport security the actual integrity guarantee, not the digest.
    """
    base = getattr(args, "url", None) or cfg.update_base_url or DEFAULT_BASE_URL
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme == "https":
        return base
    if parsed.scheme == "http" and (parsed.hostname or "") in _PLAINTEXT_HOSTS:
        return base
    raise UpdateError(
        f"refusing to fetch a release over {parsed.scheme or 'an unknown scheme'} from {base!r} — "
        "releases must come over https"
    )


def _guard_updatable() -> None:
    if materialize.current_target() is None:
        raise UpdateError(
            f"there is no installed version here to update — run {preflight.setup_door()} from a "
            f"checkout first"
        )
    if materialize.is_dev_install():
        raise UpdateError(
            f"this install points at a working tree, not a released version — update it with git "
            f"or jj, or re-run {preflight.setup_door()} without --dev to switch to versioned "
            f"installs"
        )


def check(args, cfg, out) -> int:
    """`--check`: say what is on offer and exit in a way a script can branch on."""
    release = discover(_base_url(args, cfg))
    out(f"{__version__} → {release.version}")
    if not is_newer(release.version, __version__):
        out("Already up to date.")
        return 0
    return UPDATE_AVAILABLE_EXIT


def perform(args, cfg, out, *, platform=None) -> int:
    """Fetch, verify, swap, restart, check — and undo the whole thing if the check fails."""
    _guard_updatable()
    base = _base_url(args, cfg)
    release = discover(base)
    out(f"selly-agent {__version__} → {release.version}")
    if not is_newer(release.version, __version__):
        out("Already up to date.")
        return 0

    archive = download(base, release, paths.cache_dir())
    out(f"Downloaded {release.filename} — SHA256 verified.")

    previous = materialize.current_target()
    staged = unpack(archive, paths.cache_dir() / "unpacked")
    started_at = time.time()

    # What is already wrong before we touch anything, so the gate afterwards can tell a problem
    # this update caused from one it merely inherited.
    from selly_agent import healthcheck

    failing_before = {
        result.name for result in healthcheck.run_checks(platform=platform) if result.failed
    }

    # The mode is read from the recorded config rather than from whatever config object this call
    # was handed: re-installing the job under the wrong mode would quietly move the plist and
    # turn a start-at-login daemon into one that never comes back.
    status = supervisor.gather_status(platform=platform)
    mode = status.mode
    was_running = _stop_daemon(platform)
    try:
        materialize.install_version(staged, release.version)
    except Exception:
        # The daemon is already stopped at this point, and whatever went wrong here (no disk, an
        # unmanaged `current`) is not a reason to leave the machine with no agent running. Put
        # the version that was working back up before reporting the failure.
        if was_running:
            _start_daemon(mode, platform=platform)
            out("Nothing was changed; the previous version is still running.")
        raise
    out(f"Installed versions/{release.version}; current → {release.version}.")

    if not was_running:
        # Manual mode, and it was not running before. Starting it now would be a change nobody
        # asked for, so the update stops here and says what is left to do — rather than failing a
        # health gate on a daemon that is off on purpose.
        supervisor.install(mode=mode, platform=platform)
        out(f"Updated to {release.version}. Start it to finish: selly-agent daemon start")
        materialize.prune_versions()
        return 0

    healthy = _start_daemon(mode, platform=platform)
    regressions: set = set()
    if healthy:
        from selly_agent import healthcheck

        results = healthcheck.run_checks(platform=platform)
        out(" ".join(_summarise(check_result) for check_result in results))
        # Only what this update *broke* is a reason to undo it. A machine that was already
        # signed out of Carousell, or never provisioned the rail, fails those checks before and
        # after — and rolling back for that would make every future update fail the same way,
        # forever, for a reason the update did not cause and cannot fix.
        regressions = {result.name for result in results if result.failed} - failing_before
        healthy = not regressions

    if healthy:
        materialize.prune_versions()
        _clear_cache(keep=archive)
        out(
            f"Updated to {release.version}. Previous version retained "
            f"(selly-agent update --rollback to revert)."
        )
        return 0

    reason = f"broke {', '.join(sorted(regressions))}" if regressions else "did not come up"
    out(f"{release.version} {reason} — rolling back.")
    _roll_back_to(previous, mode, out, migrated_after=started_at, platform=platform)
    out(f"Back on {materialize.current_version()}. Left {release.version} in place to inspect.")
    return 1


def _clear_cache(*, keep) -> None:
    """Drop the unpacked tree and any archive but the one just installed.

    The cache dir is documented as regenerable, and nothing regenerated it: without this every
    release ever downloaded stays on disk forever.
    """
    unpacked = paths.cache_dir() / "unpacked"
    if unpacked.is_dir():
        shutil.rmtree(unpacked, ignore_errors=True)
    for stale in paths.cache_dir().glob("selly-agent-*.tar.gz"):
        if stale != keep:
            stale.unlink(missing_ok=True)


def _summarise(result) -> str:
    return f"{checks.glyph(result.status)} {result.name}"


def _roll_back_to(target, mode: str, out, *, migrated_after: float, platform=None) -> None:
    """Put the previous version back, and with it the database that version can read.

    `migrated_after` is the moment the version being backed out arrived: a snapshot newer than
    that was taken by that version's own startup migration, which is the one being undone. A
    snapshot from before it was taken by an earlier version — possibly the target's own first
    start — and restoring one of those would throw away everything written since, for a schema
    change that never happened. Auto-rollback knows the moment exactly (when the update started);
    a rollback asked for later uses when the version being left was installed.
    """
    _stop_daemon(platform)
    materialize.swap_current(target)

    snapshot = latest_snapshot()
    if snapshot is not None and snapshot.stat().st_mtime > migrated_after:
        # The code going back cannot read the newer schema. Restoring costs whatever was written
        # since the snapshot, which is why it is said out loud rather than done quietly.
        restore_snapshot(snapshot)
        out(
            f"Restored the database as it was before the migration ({snapshot.name}). "
            "Anything written since then is not in it."
        )
    _start_daemon(mode, platform=platform)


def rollback(args, cfg, out, *, platform=None) -> int:
    """`--rollback`: the same flip, asked for rather than triggered."""
    _guard_updatable()
    target = materialize.previous_version()
    if target is None:
        raise UpdateError("there is no previous version retained to roll back to")
    leaving = materialize.current_target()
    out(f"Rolling back {materialize.current_version()} → {target.name}.")
    status = supervisor.gather_status(platform=platform)
    _roll_back_to(
        target,
        status.mode,
        out,
        # When the version being *left* was installed — not the one being returned to. The
        # target's own first start took a snapshot moments after the target's directory
        # appeared, and measuring from the target would mistake that snapshot for a migration
        # of the version being undone, restoring a database from before everything since.
        migrated_after=leaving.stat().st_mtime,
        platform=platform,
    )
    out(f"Now on {materialize.current_version()}.")
    return 0


def run(args) -> int:
    import sys

    cfg = config.load()

    def out(line: str) -> None:
        print(line)

    try:
        if getattr(args, "check", False):
            return check(args, cfg, out)
        if getattr(args, "rollback", False):
            return rollback(args, cfg, out)
        return perform(args, cfg, out)
    except (UpdateError, runtime.RuntimeSetupError) as exc:
        print(f"selly-agent: {exc}", file=sys.stderr)
        return 1
    except materialize.LayoutError as exc:
        print(f"selly-agent: {exc.message}", file=sys.stderr)
        if exc.fix:
            print(exc.fix, file=sys.stderr)
        return 1
