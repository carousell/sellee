"""The versioned layout: staging, the atomic swap, pruning, the shim, and the PATH block."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from selly_agent import paths
from selly_agent.installer import materialize
from selly_agent.installer.materialize import LayoutError

# --- staging and swapping -----------------------------------------------------------------


def test_install_version_populates_versions_and_points_current(xdg_tmp, tree) -> None:
    dest = materialize.install_version(tree, "1.0.0")
    assert dest == paths.versions_dir() / "1.0.0"
    assert (dest / "bin" / "selly-agent").is_file()
    assert (dest / "src" / "selly_agent" / "__init__.py").is_file()
    assert (dest / "README.md").is_file()
    assert paths.current().is_symlink()
    assert materialize.current_target() == dest.resolve()
    assert materialize.current_version() == "1.0.0"


def test_staging_leaves_dev_scaffolding_behind(xdg_tmp, tree) -> None:
    dest = materialize.install_version(tree, "1.0.0")
    assert not (dest / ".git").exists()
    assert not (dest / "src" / "selly_agent" / "__pycache__").exists()


def test_a_staged_version_carries_both_front_doors(xdg_tmp, tree) -> None:
    """A version has to be re-runnable on the platform it is installed on, and `update` stages the
    same tree on either — so the Windows entry point travels with every release, not just Windows
    ones."""
    dest = materialize.install_version(tree, "1.0.0")
    assert (dest / "setup").is_file()
    assert (dest / "setup.ps1").is_file()


def test_a_version_carries_the_files_it_needs_to_build_its_own_venv(xdg_tmp, tree) -> None:
    """Without these three a version cannot install its dependencies, which means it cannot be
    rolled back to either."""
    dest = materialize.install_version(tree, "1.0.0")
    for name in ("pyproject.toml", "uv.lock", ".python-version"):
        assert (dest / name).is_file(), name


def test_the_source_trees_venv_is_never_copied(xdg_tmp, tree, stub_provision) -> None:
    """A venv records absolute paths, so copying one would describe the checkout it came from.
    Each version gets its own instead."""
    dest = materialize.install_version(tree, "1.0.0")
    assert stub_provision == [dest]
    assert paths.venv_python(dest).is_file()


def test_a_version_is_provisioned_before_it_becomes_current(xdg_tmp, tree, monkeypatch) -> None:
    """A version with no dependencies must never be the live one: if provisioning fails, the
    previous version is still what current points at."""
    materialize.install_version(tree, "1.0.0")

    def explode(dest):
        raise RuntimeError("no network")

    monkeypatch.setattr(materialize.runtime, "provision", explode)
    with pytest.raises(RuntimeError):
        materialize.install_version(tree, "2.0.0")
    assert materialize.current_version() == "1.0.0"


def test_provisioning_is_injectable(xdg_tmp, tree) -> None:
    seen = []
    materialize.install_version(tree, "1.0.0", provision=seen.append)
    assert seen == [paths.versions_dir() / "1.0.0"]


def test_a_crash_between_staging_and_the_swap_leaves_current_valid(xdg_tmp, tree) -> None:
    materialize.install_version(tree, "1.0.0")
    # The next version stages but never swaps — the shape of a crash mid-install.
    materialize.stage_version(tree, "2.0.0")
    assert materialize.current_version() == "1.0.0"
    assert (paths.current() / "bin" / "selly-agent").is_file()


def test_restaging_the_same_version_refreshes_it_in_place(xdg_tmp, tree) -> None:
    materialize.install_version(tree, "0.1.0.dev0")
    (tree / "bin" / "selly-agent").write_text("#!/usr/bin/env python3\n# newer\n")
    materialize.install_version(tree, "0.1.0.dev0")
    live = paths.versions_dir() / "0.1.0.dev0"
    assert "# newer" in (live / "bin" / "selly-agent").read_text()
    assert materialize.current_version() == "0.1.0.dev0"
    # No debris from the swap-aside.
    assert [p.name for p in paths.versions_dir().iterdir()] == ["0.1.0.dev0"]


def test_a_tree_missing_its_package_is_refused(xdg_tmp, tmp_path) -> None:
    empty = tmp_path / "not-a-tree"
    empty.mkdir()
    with pytest.raises(LayoutError) as caught:
        materialize.install_version(empty, "1.0.0")
    assert "bin/ is missing" in caught.value.message


def test_a_real_directory_at_current_is_refused_with_a_remedy(xdg_tmp, tree) -> None:
    paths.ensure_runtime_dirs()
    paths.current().mkdir(parents=True)
    with pytest.raises(LayoutError) as caught:
        materialize.install_version(tree, "1.0.0")
    assert "real directory" in caught.value.message
    assert "mv " in caught.value.fix
    # Refused, not eaten.
    assert paths.current().is_dir() and not paths.current().is_symlink()


def test_dev_install_points_current_at_the_working_tree(xdg_tmp, tree) -> None:
    materialize.install_dev(tree)
    assert materialize.current_target() == tree.resolve()
    assert materialize.current_version() is None
    assert materialize.is_dev_install() is True


def test_a_versioned_install_is_not_a_dev_install(xdg_tmp, tree) -> None:
    materialize.install_version(tree, "1.0.0")
    assert materialize.is_dev_install() is False


def test_swapping_never_leaves_current_absent(xdg_tmp, tree) -> None:
    materialize.install_version(tree, "1.0.0")
    original = os.readlink(paths.current())
    materialize.install_version(tree, "2.0.0")
    assert os.readlink(paths.current()) != original
    assert paths.current().is_symlink()


# --- retention ------------------------------------------------------------------------------


def test_prune_keeps_the_newest_three_and_never_the_live_one(xdg_tmp, tree) -> None:
    for index in range(5):
        materialize.install_version(tree, f"1.0.{index}")
        # Distinct mtimes, so "most recently installed" is unambiguous.
        os.utime(paths.versions_dir() / f"1.0.{index}", (1000 + index, 1000 + index))
    materialize.swap_current(paths.versions_dir() / "1.0.4")

    removed = materialize.prune_versions(keep=3)

    assert sorted(removed) == ["1.0.0", "1.0.1"]
    assert sorted(p.name for p in materialize.installed_versions()) == ["1.0.2", "1.0.3", "1.0.4"]
    assert materialize.current_version() == "1.0.4"


def test_previous_version_is_where_a_rollback_goes(xdg_tmp, tree) -> None:
    materialize.install_version(tree, "1.0.0")
    os.utime(paths.versions_dir() / "1.0.0", (1000, 1000))
    materialize.install_version(tree, "2.0.0")
    os.utime(paths.versions_dir() / "2.0.0", (2000, 2000))
    assert materialize.previous_version().name == "1.0.0"


def test_previous_version_is_none_on_a_first_install(xdg_tmp, tree) -> None:
    materialize.install_version(tree, "1.0.0")
    assert materialize.previous_version() is None


# --- the shim -------------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="the shim is a .cmd there; test_pointer covers it")
def test_shim_links_through_current_so_updates_do_not_rewrite_it(xdg_tmp, tree) -> None:
    materialize.install_version(tree, "1.0.0")
    shim = materialize.install_shim()
    assert shim.is_symlink()
    assert os.readlink(shim) == str(paths.current() / "bin" / "selly-agent")
    assert shim.resolve().is_file()

    materialize.install_version(tree, "2.0.0")
    # Untouched, and now resolving to the new version through current.
    assert shim.resolve() == (paths.versions_dir() / "2.0.0" / "bin" / "selly-agent").resolve()


@pytest.mark.skipif(os.name == "nt", reason="the shim is a .cmd there; test_pointer covers it")
def test_shim_install_is_idempotent(xdg_tmp, tree) -> None:
    materialize.install_version(tree, "1.0.0")
    materialize.install_shim()
    materialize.install_shim()
    assert paths.shim_path().is_symlink()


def test_shim_refuses_to_clobber_a_real_file(xdg_tmp, tree) -> None:
    materialize.install_version(tree, "1.0.0")
    paths.shim_path().parent.mkdir(parents=True, exist_ok=True)
    paths.shim_path().write_text("someone else's script\n")
    with pytest.raises(LayoutError):
        materialize.install_shim()
    assert paths.shim_path().read_text() == "someone else's script\n"


@pytest.mark.skipif(os.name == "nt", reason="the shim is a .cmd there; test_pointer covers it")
def test_remove_shim_only_removes_our_own(xdg_tmp, tree, tmp_path) -> None:
    materialize.install_version(tree, "1.0.0")
    materialize.install_shim()
    assert materialize.remove_shim() is True
    assert not paths.shim_path().exists()

    foreign_target = tmp_path / "elsewhere"
    foreign_target.write_text("")
    paths.shim_path().symlink_to(foreign_target)
    assert materialize.remove_shim() is False
    assert paths.shim_path().is_symlink()


# --- PATH -----------------------------------------------------------------------------------


def test_user_bin_on_path_reads_the_invoking_shells_path(xdg_tmp) -> None:
    bin_dir = paths.user_bin_dir()
    assert materialize.user_bin_on_path(f"/usr/bin{os.pathsep}{bin_dir}") is True
    assert materialize.user_bin_on_path("/usr/bin:/bin") is False
    assert materialize.user_bin_on_path("") is False


def test_rc_block_is_appended_once_and_is_a_no_op_on_re_run(tmp_path) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text("export EDITOR=vim\n")

    assert materialize.add_rc_block(rc) is True
    after_first = rc.read_text()
    assert "export EDITOR=vim" in after_first
    assert after_first.count(materialize.RC_MARKER_START) == 1

    assert materialize.add_rc_block(rc) is False
    assert rc.read_text() == after_first


def test_rc_block_is_created_for_a_shell_with_no_rc_file_yet(tmp_path) -> None:
    rc = tmp_path / ".bash_profile"
    assert materialize.add_rc_block(rc) is True
    assert materialize.rc_block_present(rc.read_text())


def test_removing_the_rc_block_restores_the_surrounding_file(tmp_path) -> None:
    rc = tmp_path / ".zshrc"
    original = "export EDITOR=vim\nalias ll='ls -l'\n"
    rc.write_text(original)
    materialize.add_rc_block(rc)

    assert materialize.remove_rc_block(rc) is True
    assert rc.read_text() == original
    assert materialize.remove_rc_block(rc) is False


def test_the_user_path_entry_is_added_once_and_removed_exactly(xdg_tmp, monkeypatch) -> None:
    """The Windows counterpart of the rc block. Registry access is stubbed — what is under test is
    the arithmetic on the PATH value, which is where an install can damage something."""
    stored = {"value": r"C:\Tools;%USERPROFILE%\bin"}
    monkeypatch.setattr(materialize, "_read_user_path", lambda: stored["value"])
    monkeypatch.setattr(
        materialize, "_write_user_path", lambda value: stored.__setitem__("value", value)
    )
    ours = str(paths.user_bin_dir())

    assert materialize.add_user_path_entry() is True
    assert stored["value"].split(";") == [r"C:\Tools", r"%USERPROFILE%\bin", ours]
    assert materialize.add_user_path_entry() is False  # idempotent

    assert materialize.remove_user_path_entry() is True
    assert stored["value"] == r"C:\Tools;%USERPROFILE%\bin"  # everything else untouched
    assert materialize.remove_user_path_entry() is False


def test_an_entry_differing_only_in_case_or_a_trailing_slash_is_not_added_twice(
    xdg_tmp, monkeypatch
) -> None:
    """Windows paths are case-insensitive and people's PATH entries end in a backslash as often as
    not, so a naive compare would append a second copy of the same directory on every install."""
    ours = str(paths.user_bin_dir())
    stored = {"value": f"{ours.upper()}\\"}
    monkeypatch.setattr(materialize, "_read_user_path", lambda: stored["value"])
    monkeypatch.setattr(materialize, "_write_user_path", lambda value: pytest.fail("wrote anyway"))

    assert materialize.add_user_path_entry() is False


def test_removing_a_block_leaves_anything_written_after_it_alone(tmp_path) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text("first\n")
    materialize.add_rc_block(rc)
    rc.write_text(rc.read_text() + "later addition\n")

    materialize.remove_rc_block(rc)

    assert rc.read_text() == "first\nlater addition\n"


def test_rc_target_is_only_offered_for_the_shells_we_can_be_right_about(xdg_tmp) -> None:
    assert materialize.shell_rc_target("/bin/zsh").name == ".zshrc"
    assert materialize.shell_rc_target("/bin/bash").name == ".bash_profile"
    # fish never reads ~/.profile and an `export` line is not its syntax, so there is no file
    # here we could write that would do anything.
    assert materialize.shell_rc_target("/usr/local/bin/fish") is None
    assert materialize.shell_rc_target("/bin/ksh") is None
    assert materialize.shell_rc_target("") is None


def test_the_shell_name_comes_from_the_login_shell(xdg_tmp, monkeypatch) -> None:
    assert materialize.shell_name("/usr/local/bin/fish") == "fish"
    assert materialize.shell_name("") == ""
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert materialize.shell_name() == "zsh"
    monkeypatch.delenv("SHELL")
    assert materialize.shell_name() == ""


# --- the preview ----------------------------------------------------------------------------


def test_layout_preview_names_every_root_it_will_write(xdg_tmp) -> None:
    from selly_agent.platform.macos import MacOSPlatform

    text = "\n".join(materialize.layout_preview(platform=MacOSPlatform()))
    for location in (
        paths.data_root(),
        paths.state_dir(),
        paths.config_dir(),
        paths.cache_dir(),
        paths.shim_path(),
    ):
        assert str(location) in text


def test_the_shim_from_a_dev_install_is_still_ours_to_remove(xdg_tmp, tree) -> None:
    # A dev shim resolves into the checkout, not the data root, so ownership cannot be decided
    # by where it lands — it is decided by what we wrote.
    materialize.install_dev(tree)
    materialize.install_shim()
    assert materialize.remove_shim() is True
    assert not paths.shim_path().exists()


def test_install_refuses_to_take_a_name_a_foreign_symlink_already_holds(
    xdg_tmp, tree, tmp_path
) -> None:
    # The mirror of uninstall's care not to delete someone else's shim: taking it in the first
    # place is the half that cannot be given back.
    materialize.install_version(tree, "1.0.0")
    theirs = tmp_path / "their-tool"
    theirs.write_text("")
    paths.shim_path().parent.mkdir(parents=True, exist_ok=True)
    paths.shim_path().symlink_to(theirs)

    with pytest.raises(LayoutError):
        materialize.install_shim()
    assert os.readlink(paths.shim_path()) == str(theirs)


def test_an_unterminated_block_is_left_alone_rather_than_eating_the_file(tmp_path) -> None:
    # Someone edited their rc file and lost the closing fence. Skipping to end-of-file would
    # silently delete everything they wrote after our block.
    rc = tmp_path / ".zshrc"
    rc.write_text(f"first\n{materialize.RC_MARKER_START}\nexport PATH=x\nalias ll='ls -l'\nlater\n")
    original = rc.read_text()

    assert materialize.remove_rc_block(rc) is False
    assert rc.read_text() == original


def test_prune_keeps_a_full_set_when_there_is_no_live_version(xdg_tmp, tree) -> None:
    for index in range(5):
        materialize.stage_version(tree, f"1.0.{index}")
        os.utime(paths.versions_dir() / f"1.0.{index}", (1000 + index, 1000 + index))

    materialize.prune_versions(keep=3)

    assert sorted(p.name for p in materialize.installed_versions()) == ["1.0.2", "1.0.3", "1.0.4"]


def test_a_leftover_from_a_crashed_swap_is_swept(xdg_tmp, tree) -> None:
    materialize.install_version(tree, "1.0.0")
    orphan = paths.versions_dir() / "0.9.0.old"
    orphan.mkdir()
    (orphan / "junk").write_text("")

    materialize.stage_version(tree, "1.0.1")

    assert not orphan.exists()


def test_rc_candidates_cover_every_shell_we_might_have_written_to(xdg_tmp) -> None:
    names = {path.name for path in materialize.rc_candidates()}
    assert {".zshrc", ".bash_profile", ".profile"} <= names


def test_the_release_archive_carries_what_a_version_directory_needs() -> None:
    """`make dist` and staging must agree. They are separate lists in separate languages, so a
    file added to one and not the other yields a release that installs but cannot run."""
    makefile = (Path(__file__).resolve().parents[1] / "Makefile").read_text()
    packed = " ".join(line for line in makefile.splitlines() if line.strip().startswith("@cp"))
    for name in materialize.VERSION_FILES:
        if name == "LICENSE":
            continue  # copied conditionally; absent from the repo today
        assert name in packed, f"{name} is staged into a version but not packed by `make dist`"
    for name in materialize.VERSION_DIRS:
        assert name in packed, name
