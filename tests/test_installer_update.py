"""`sellee update`: discovery, the digest gate, the swap, and the rollback matrix.

The release is real — a tarball built here and served from a local HTTP server — so the fetch,
the checksum, the extraction and the version swap are all the production code. What is faked is
everything a test cannot have: launchctl, a daemon that heartbeats, and the health verdict that
decides whether the new version stays.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tarfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest
from tests.test_supervisor import FakePlatform

from sellee import healthcheck, heartbeat, paths, supervisor
from sellee.config import Config
from sellee.installer import checks, materialize
from sellee.installer import update as update_mod
from sellee.installer.update import Release, UpdateError


def build_release(into, version: str, *, marker: str = "") -> str:
    """Build a release tarball plus its SHA256SUMS in `into`, and answer the digest."""
    into.mkdir(parents=True, exist_ok=True)
    stage = into / f"stage-{version}"
    root = stage / f"sellee-{version}"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "sellee").write_text("#!/usr/bin/env python3\n")
    (root / "src" / "sellee").mkdir(parents=True)
    (root / "src" / "sellee" / "__init__.py").write_text(f"__version__ = {version!r}\n{marker}")

    name = f"sellee-{version}.tar.gz"
    with tarfile.open(into / name, "w:gz") as tar:
        tar.add(root, arcname=root.name)
    digest = hashlib.sha256((into / name).read_bytes()).hexdigest()
    (into / "SHA256SUMS").write_text(f"{digest}  {name}\n")
    return digest


@pytest.fixture
def served(tmp_path):
    """A directory served over HTTP, as a release host would."""
    root = tmp_path / "releases"
    root.mkdir()
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield root, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def write_db(path, marker: str) -> None:
    """A real SQLite file carrying a marker, so a restore is asserted on content and not bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS marker (note TEXT)")
        conn.execute("DELETE FROM marker")
        conn.execute("INSERT INTO marker (note) VALUES (?)", (marker,))
        conn.commit()
    finally:
        conn.close()


def read_db(path) -> str:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT note FROM marker").fetchone()[0]
    finally:
        conn.close()


class Args:
    def __init__(self, *, url=None, check=False, rollback=False):
        self.url = url
        self.check = check
        self.rollback = rollback


# --- pure ----------------------------------------------------------------------------------------


def test_version_ordering_puts_a_dev_build_below_its_own_release() -> None:
    assert update_mod.is_newer("0.2.0", "0.1.0.dev0")
    assert update_mod.is_newer("0.1.0", "0.1.0.dev0")
    assert not update_mod.is_newer("0.1.0.dev0", "0.1.0")
    assert not update_mod.is_newer("0.1.0", "0.1.0")
    assert not update_mod.is_newer("0.9.0", "1.0.0")
    assert update_mod.is_newer("1.0.1", "1.0.0")


def test_sums_parsing_ignores_anything_that_is_not_a_checksum_line() -> None:
    text = "# a comment\n\n" + "a" * 64 + "  sellee-1.0.0.tar.gz\nnot a line\n"
    assert update_mod.parse_sums(text) == {"sellee-1.0.0.tar.gz": "a" * 64}


def test_the_release_is_read_out_of_its_own_checksum_file() -> None:
    release = update_mod.release_from_sums("b" * 64 + "  sellee-2.3.4.tar.gz\n")
    assert release == Release(version="2.3.4", filename="sellee-2.3.4.tar.gz", digest="b" * 64)


def test_several_releases_in_one_sums_file_resolve_to_the_highest() -> None:
    text = "a" * 64 + "  sellee-2.0.0.tar.gz\n" + "b" * 64 + "  sellee-10.0.0.tar.gz\n"
    assert update_mod.release_from_sums(text).version == "10.0.0"


def test_a_sums_file_with_no_archive_is_refused() -> None:
    with pytest.raises(UpdateError):
        update_mod.release_from_sums("c" * 64 + "  something-else.zip\n")


# --- fetch and verify --------------------------------------------------------------------------


def test_discovery_reads_the_version_from_the_host(xdg_tmp, served) -> None:
    root, base = served
    build_release(root, "3.0.0")
    assert update_mod.discover(base).version == "3.0.0"


def test_a_tampered_download_is_refused_and_left_for_inspection(xdg_tmp, served) -> None:
    root, base = served
    build_release(root, "3.0.0")
    archive = root / "sellee-3.0.0.tar.gz"
    archive.write_bytes(archive.read_bytes() + b"tampered")

    release = update_mod.discover(base)
    with pytest.raises(UpdateError) as caught:
        update_mod.download(base, release, paths.cache_dir())

    assert "does not match its published checksum" in str(caught.value)
    assert (paths.cache_dir() / release.filename).exists()  # kept as evidence


def test_an_archive_that_would_escape_its_destination_is_refused(xdg_tmp, tmp_path) -> None:
    archive = tmp_path / "evil.tar.gz"
    payload = tmp_path / "payload"
    payload.write_text("x")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload, arcname="../escaped")

    with pytest.raises(UpdateError) as caught:
        update_mod.unpack(archive, tmp_path / "out")
    assert "outside" in str(caught.value)


# --- --check ----------------------------------------------------------------------------------


def test_check_exits_ten_when_there_is_something_to_install(xdg_tmp, served) -> None:
    root, base = served
    build_release(root, "9.9.9")
    lines = []
    assert update_mod.check(Args(url=base), Config(), lines.append) == 10
    assert any("9.9.9" in line for line in lines)


def test_check_exits_zero_when_current(xdg_tmp, served) -> None:
    from sellee import __version__

    root, base = served
    build_release(root, __version__)
    lines = []
    assert update_mod.check(Args(url=base), Config(), lines.append) == 0
    assert "Already up to date." in lines


# --- the full update ------------------------------------------------------------------------------


@pytest.fixture
def installed(xdg_tmp, tmp_path, monkeypatch):
    """A machine with a version installed and a daemon that is running."""
    tree = tmp_path / "installed"
    (tree / "bin").mkdir(parents=True)
    (tree / "bin" / "sellee").write_text("#!/usr/bin/env python3\n")
    (tree / "src" / "sellee").mkdir(parents=True)
    (tree / "src" / "sellee" / "__init__.py").write_text("__version__ = '0.1.0'\n")
    materialize.install_version(tree, "0.1.0")

    platform = FakePlatform()
    supervisor.install(mode="login-start", platform=platform)
    monkeypatch.setattr(heartbeat, "wait_fresh", lambda path, **kwargs: True)
    monkeypatch.setattr(
        healthcheck, "run_checks", lambda platform=None: [checks.ok("daemon", "up")]
    )
    return platform


def test_a_successful_update_swaps_current_and_keeps_the_old_version(installed, served) -> None:
    root, base = served
    build_release(root, "0.2.0")
    lines = []

    assert update_mod.perform(Args(url=base), Config(), lines.append, platform=installed) == 0

    assert materialize.current_version() == "0.2.0"
    assert (paths.versions_dir() / "0.1.0").is_dir()  # rollback has somewhere to go
    assert any("SHA256 verified" in line for line in lines)
    assert any("--rollback to revert" in line for line in lines)


def test_the_plist_is_re_rendered_so_it_names_the_new_version(installed, served) -> None:
    root, base = served
    build_release(root, "0.2.0")
    update_mod.perform(Args(url=base), Config(), lambda line: None, platform=installed)

    plist = (paths.launch_agents_dir(platform=installed) / "com.sellee.agent.plist").read_text()
    assert str(paths.current() / "bin" / "sellee") in plist
    assert supervisor.MARKER in plist


def test_updating_to_the_version_already_installed_does_nothing(installed, served) -> None:
    from sellee import __version__

    root, base = served
    build_release(root, __version__)
    lines = []
    assert update_mod.perform(Args(url=base), Config(), lines.append, platform=installed) == 0
    assert "Already up to date." in lines


def test_a_dev_install_refuses_to_update(xdg_tmp, tmp_path, served) -> None:
    tree = tmp_path / "checkout"
    (tree / "bin").mkdir(parents=True)
    (tree / "src").mkdir(parents=True)
    materialize.install_dev(tree)

    with pytest.raises(UpdateError) as caught:
        update_mod.perform(Args(url=served[1]), Config(), lambda line: None)
    assert "working tree" in str(caught.value)


def test_a_container_install_is_told_to_rebuild_its_image(container, xdg_tmp, served) -> None:
    """A version directory swap is not how this install gets a new version, and "run ./setup from
    a checkout" is advice that leads nowhere from inside a container."""
    with pytest.raises(UpdateError) as caught:
        update_mod.perform(Args(url=served[1]), Config(), lambda line: None)
    message = str(caught.value)
    assert "a new image" in message
    assert "./setup" not in message  # advice that leads nowhere from inside a container
    assert "docker" not in message.lower()  # nor an engine we did not choose


def test_the_release_notice_names_the_command_this_deployment_updates_with(
    container, installed, served, store, bus
) -> None:
    root, base = served
    build_release(root, "9.9.9")
    update_mod.update_probe(
        store=store, bus=bus, config_obj=Config(update_base_url=base), seen=set()
    )
    notice = store.claim_queued_notices(1)[0]["text"]
    assert update_mod.CONTAINER_UPDATE_HOW in notice
    assert "sellee update" not in notice  # the verb that refuses here


def test_a_manual_daemon_that_was_not_running_is_not_started_by_an_update(
    xdg_tmp, tmp_path, monkeypatch, served
) -> None:
    tree = tmp_path / "installed"
    (tree / "bin").mkdir(parents=True)
    (tree / "src" / "sellee").mkdir(parents=True)
    materialize.install_version(tree, "0.1.0")
    platform = FakePlatform()
    supervisor.install(mode="manual", platform=platform)  # manual: installed, not registered

    root, base = served
    build_release(root, "0.2.0")
    lines = []
    rc = update_mod.perform(
        Args(url=base), Config(daemon_mode="manual"), lines.append, platform=platform
    )

    assert rc == 0
    assert materialize.current_version() == "0.2.0"
    assert not platform.is_registered("com.sellee.agent")  # still off, as it was
    assert any("Start it to finish" in line for line in lines)


# --- rollback ---------------------------------------------------------------------------------


def _health(*rounds):
    """Scripted health verdicts: the pre-update read first, then the post-update one."""
    answers = iter(rounds)
    return lambda platform=None: next(answers)


def test_a_version_that_breaks_something_is_rolled_back(installed, served, monkeypatch) -> None:
    root, base = served
    build_release(root, "0.2.0")
    monkeypatch.setattr(
        healthcheck,
        "run_checks",
        _health([checks.ok("daemon", "up")], [checks.fail("daemon", "not running")]),
    )
    lines = []

    rc = update_mod.perform(Args(url=base), Config(), lines.append, platform=installed)

    assert rc == 1
    assert materialize.current_version() == "0.1.0"
    # The failed version stays on disk: it is the evidence for why it failed.
    assert (paths.versions_dir() / "0.2.0").is_dir()
    assert any("broke daemon" in line for line in lines)


def test_a_problem_that_predates_the_update_does_not_undo_it(
    installed, served, monkeypatch
) -> None:
    # A machine that never provisioned the rail, or whose claude session expired, fails those
    # checks before and after. Rolling back for that would make every future update fail the
    # same way forever, for a reason the update neither caused nor can fix.
    root, base = served
    build_release(root, "0.2.0")
    already_broken = [checks.ok("daemon", "up"), checks.fail("carousell.ai key", "missing")]
    monkeypatch.setattr(healthcheck, "run_checks", _health(already_broken, already_broken))

    rc = update_mod.perform(Args(url=base), Config(), lambda line: None, platform=installed)

    assert rc == 0
    assert materialize.current_version() == "0.2.0"


def test_a_rollback_restores_the_database_only_when_the_new_version_migrated_it(
    installed, served, monkeypatch
) -> None:
    root, base = served
    build_release(root, "0.2.0")
    paths.ensure_state_dirs()
    write_db(paths.sellee_db(), "after the migration")

    # The migration runner snapshots the database when — and only when — something is pending.
    # Simulate that happening during the new version's first start.
    def start_and_migrate(mode, *, platform=None):
        write_db(paths.backups_dir() / "sellee-2000-pre-0009.db", "before the migration")
        return True

    monkeypatch.setattr(update_mod, "_start_daemon", start_and_migrate)
    monkeypatch.setattr(
        healthcheck,
        "run_checks",
        _health([checks.ok("daemon", "up")], [checks.fail("daemon", "wedged")]),
    )
    lines = []

    update_mod.perform(Args(url=base), Config(), lines.append, platform=installed)

    assert read_db(paths.sellee_db()) == "before the migration"
    assert any("Anything written since then is not in it" in line for line in lines)


def test_a_rollback_without_a_migration_leaves_the_database_alone(
    installed, served, monkeypatch
) -> None:
    root, base = served
    build_release(root, "0.2.0")
    paths.ensure_state_dirs()
    write_db(paths.sellee_db(), "untouched")
    write_db(paths.backups_dir() / "sellee-1000-pre-0008.db", "an older snapshot")

    monkeypatch.setattr(
        healthcheck,
        "run_checks",
        _health([checks.ok("daemon", "up")], [checks.fail("daemon", "wedged")]),
    )

    update_mod.perform(Args(url=base), Config(), lambda line: None, platform=installed)

    # No new snapshot appeared, so no migration ran, so the database is the one both versions read.
    assert read_db(paths.sellee_db()) == "untouched"


def test_rollback_by_hand_goes_back_one_version(installed, served) -> None:
    root, base = served
    build_release(root, "0.2.0")
    update_mod.perform(Args(url=base), Config(), lambda line: None, platform=installed)
    assert materialize.current_version() == "0.2.0"

    lines = []
    assert update_mod.rollback(Args(rollback=True), Config(), lines.append, platform=installed) == 0
    assert materialize.current_version() == "0.1.0"


def test_rollback_refuses_when_there_is_nowhere_to_go(installed) -> None:
    with pytest.raises(UpdateError) as caught:
        update_mod.rollback(Args(rollback=True), Config(), lambda line: None, platform=installed)
    assert "no previous version" in str(caught.value)


# --- the daemon's probe ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.notices = []
        self.events = []

    def queue_notice(self, text, **kwargs):
        self.notices.append(text)

    def publish(self, kind, payload, **kwargs):
        self.events.append((kind, payload))


def test_the_probe_notices_a_new_release_once(installed, served) -> None:
    root, base = served
    build_release(root, "9.9.9")
    recorder = _Recorder()
    seen = set()
    cfg = Config(update_base_url=base)

    update_mod.update_probe(store=recorder, bus=recorder, config_obj=cfg, seen=seen)
    update_mod.update_probe(store=recorder, bus=recorder, config_obj=cfg, seen=seen)

    assert len(recorder.notices) == 1
    assert "9.9.9" in recorder.notices[0]
    assert recorder.events[0][0] == "update.available"


def test_the_probe_says_nothing_on_a_dev_install(xdg_tmp, tmp_path, served) -> None:
    # Every real release outranks a .dev version, so a developer would be nagged forever.
    root, base = served
    build_release(root, "9.9.9")
    tree = tmp_path / "checkout"
    (tree / "bin").mkdir(parents=True)
    (tree / "src").mkdir(parents=True)
    materialize.install_dev(tree)

    recorder = _Recorder()
    update_mod.update_probe(
        store=recorder, bus=recorder, config_obj=Config(update_base_url=base), seen=set()
    )
    assert recorder.notices == []


def test_an_unreachable_release_host_is_not_an_error_the_seller_hears(installed) -> None:
    recorder = _Recorder()
    update_mod.update_probe(
        store=recorder,
        bus=recorder,
        config_obj=Config(update_base_url="http://127.0.0.1:1/nowhere"),
        seen=set(),
    )
    assert recorder.notices == []


def test_a_failure_mid_swap_puts_the_running_version_back(installed, served, monkeypatch) -> None:
    # Between stopping the daemon and starting it again there is a window where a failure would
    # otherwise leave the machine with no agent running at all.
    root, base = served
    build_release(root, "0.2.0")
    monkeypatch.setattr(
        materialize,
        "install_version",
        lambda tree, version: (_ for _ in ()).throw(OSError("no space left on device")),
    )
    restarted = []
    monkeypatch.setattr(
        update_mod, "_start_daemon", lambda mode, platform=None: restarted.append(mode) or True
    )
    lines = []

    with pytest.raises(OSError):
        update_mod.perform(Args(url=base), Config(), lines.append, platform=installed)

    assert restarted == ["login-start"]  # back up, in the mode it was in
    assert any("still running" in line for line in lines)


def test_a_manual_rollback_restores_a_database_the_old_code_can_read(
    installed, served, monkeypatch
) -> None:
    # The failure mode this exists for: update succeeds and migrates, and the rollback a day
    # later flips the symlink but leaves the database on the newer schema.
    root, base = served
    build_release(root, "0.2.0")
    paths.ensure_state_dirs()
    write_db(paths.sellee_db(), "migrated by 0.2.0")

    def start_and_migrate(mode, *, platform=None):
        write_db(paths.backups_dir() / "sellee-3000-pre-0009.db", "as 0.1.0 left it")
        return True

    monkeypatch.setattr(update_mod, "_start_daemon", start_and_migrate)
    update_mod.perform(Args(url=base), Config(), lambda line: None, platform=installed)
    assert materialize.current_version() == "0.2.0"

    lines = []
    update_mod.rollback(Args(rollback=True), Config(), lines.append, platform=installed)

    assert materialize.current_version() == "0.1.0"
    assert read_db(paths.sellee_db()) == "as 0.1.0 left it"
    assert any("Restored the database" in line for line in lines)


def test_a_manual_rollback_with_no_migration_since_leaves_the_database_alone(
    installed, served, monkeypatch
) -> None:
    # The snapshot here is the one 0.1.0's own first start took: written moments after 0.1.0
    # was installed, holding almost nothing. 0.2.0 migrated nothing, so rolling back to 0.1.0
    # must not "restore" that near-empty day-one file over everything written since — which is
    # exactly what measuring from the *target's* install time would do.
    root, base = served
    build_release(root, "0.2.0")
    paths.ensure_state_dirs()
    write_db(paths.backups_dir() / "sellee-1000-pre-0001.db", "as 0.1.0 first found it")
    write_db(paths.sellee_db(), "weeks of listings and threads")

    update_mod.perform(Args(url=base), Config(), lambda line: None, platform=installed)
    lines = []
    update_mod.rollback(Args(rollback=True), Config(), lines.append, platform=installed)

    assert materialize.current_version() == "0.1.0"
    assert read_db(paths.sellee_db()) == "weeks of listings and threads"
    assert not any("Restored the database" in line for line in lines)


def test_pre_release_versions_sort_below_their_release() -> None:
    assert update_mod.is_newer("1.2.3", "1.2.3-rc1")
    assert not update_mod.is_newer("1.2.3-rc1", "1.2.3")
    assert update_mod.is_newer("1.2.3.4", "1.2.3")
    assert update_mod.is_newer("1.2.3", "1.2.3b2")
    assert not update_mod.is_newer("1.2.3", "1.2.4-rc1")


def test_a_release_channel_must_be_https(xdg_tmp) -> None:
    # Everything fetched here is executed, and the checksums travel the same channel as the
    # archive they vouch for — so whoever controls an unencrypted channel controls both.
    with pytest.raises(UpdateError) as caught:
        update_mod._base_url(Args(url="http://releases.example.test"), Config())
    assert "https" in str(caught.value)
    # Loopback is exempt: there is no network in between to intercept.
    assert update_mod._base_url(Args(url="http://127.0.0.1:9/x"), Config())


def test_updating_with_nothing_installed_says_so_instead_of_crashing(xdg_tmp, served) -> None:
    with pytest.raises(UpdateError) as caught:
        update_mod.perform(Args(url=served[1]), Config(), lambda line: None)
    assert "no installed version" in str(caught.value)


def test_a_successful_update_clears_the_download_cache(installed, served) -> None:
    root, base = served
    build_release(root, "0.2.0")
    stale = paths.cache_dir()
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "sellee-0.0.9.tar.gz").write_text("an old download")

    update_mod.perform(Args(url=base), Config(), lambda line: None, platform=installed)

    assert not (stale / "sellee-0.0.9.tar.gz").exists()
    assert not (stale / "unpacked").exists()
    assert (stale / "sellee-0.2.0.tar.gz").exists()
