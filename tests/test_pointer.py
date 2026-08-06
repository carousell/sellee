"""The `current` pointer and the command shim: swap, read, discard, and never follow.

The property every test here is really about: a pointer is a thing you replace or remove, never a
thing you delete through. Getting that wrong takes the installed version with it.
"""

from __future__ import annotations

import os

import pytest

from selly_agent import pointer


def _version(tmp_path, name: str):
    target = tmp_path / "versions" / name
    (target / "bin").mkdir(parents=True)
    (target / "bin" / "selly-agent").write_text("#!/usr/bin/env python3\n")
    return target


def test_a_swap_points_somewhere_new_without_touching_either_target(tmp_path) -> None:
    one, two = _version(tmp_path, "1.0.0"), _version(tmp_path, "2.0.0")
    current = tmp_path / "current"

    pointer.swap(current, one)
    assert pointer.read(current) == one.resolve()

    pointer.swap(current, two)
    assert pointer.read(current) == two.resolve()
    assert (one / "bin" / "selly-agent").exists()  # the version it left is untouched


def test_a_real_directory_is_not_a_pointer(tmp_path) -> None:
    """The check that keeps an install from eating a tree somebody else put there."""
    plain = tmp_path / "current"
    plain.mkdir()

    assert pointer.is_pointer(plain) is False
    assert pointer.read(plain) is None


def test_reading_something_that_is_not_there_is_none(tmp_path) -> None:
    assert pointer.read(tmp_path / "nothing") is None


def test_discarding_a_pointer_leaves_what_it_named(tmp_path) -> None:
    version = _version(tmp_path, "1.0.0")
    current = tmp_path / "current"
    pointer.swap(current, version)

    pointer.discard(current)

    assert not current.exists()
    assert (version / "bin" / "selly-agent").exists()


def test_swapping_over_a_stale_staging_entry_still_works(tmp_path) -> None:
    """A crash mid-swap can leave the staging name behind; the next swap must not trip on it."""
    version = _version(tmp_path, "1.0.0")
    current = tmp_path / "current"
    (tmp_path / "current.new").write_text("left over")

    pointer.swap(current, version)

    assert pointer.read(current) == version.resolve()


# --- the shim ---------------------------------------------------------------------------------


def test_the_shim_names_the_launcher_it_was_given(tmp_path) -> None:
    launcher = _version(tmp_path, "1.0.0") / "bin" / "selly-agent"
    shim = tmp_path / "bin" / f"selly-agent{pointer.SHIM_SUFFIX}"

    pointer.write_shim(shim, launcher, "/usr/bin/python3")

    assert shim.exists()
    assert pointer.shim_target(shim) == launcher


def test_a_shim_is_replaced_rather_than_refused_on_reinstall(tmp_path) -> None:
    first = _version(tmp_path, "1.0.0") / "bin" / "selly-agent"
    second = _version(tmp_path, "2.0.0") / "bin" / "selly-agent"
    shim = tmp_path / "bin" / f"selly-agent{pointer.SHIM_SUFFIX}"

    pointer.write_shim(shim, first, "/usr/bin/python3")
    pointer.write_shim(shim, second, "/usr/bin/python3")

    assert pointer.shim_target(shim) == second


def test_something_that_is_not_our_shim_reads_as_unknown(tmp_path) -> None:
    """install_shim refuses a name it did not write, and this is the question it asks."""
    foreign = tmp_path / "selly-agent"
    foreign.write_text("#!/bin/sh\necho not ours\n")

    assert pointer.shim_target(foreign) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink shim")
def test_on_posix_the_shim_is_a_link_to_the_launcher(tmp_path) -> None:
    launcher = _version(tmp_path, "1.0.0") / "bin" / "selly-agent"
    shim = tmp_path / "bin" / "selly-agent"

    pointer.write_shim(shim, launcher, "/usr/bin/python3")

    assert shim.is_symlink()
    assert pointer.SHIM_SUFFIX == ""


def test_the_windows_shim_is_written_in_the_code_page_cmd_reads(monkeypatch) -> None:
    """cmd.exe parses a batch file in the console code page. Written in the ANSI one instead, a
    shim under an accented user directory names a target its own `if exist` never matches.

    The choice is asserted rather than the bytes: the oem codec exists only on Windows, so a
    POSIX run cannot round-trip through it to check the result.
    """
    monkeypatch.setattr(pointer, "_WINDOWS", True)
    # A real page number, never the stdlib's "oem" alias: that alias passes the CP_OEMCP
    # sentinel to the code-page codec as if it were a page, and every encode through it fails.
    assert pointer._shim_encoding() != "oem"
    if os.name == "nt":
        assert pointer._shim_encoding().startswith("cp")


def test_the_shim_is_written_as_bytes_not_through_a_text_stream(tmp_path) -> None:
    """The code-page codecs have no working incremental encoder, so a text-mode write through
    Path.write_text raises on this shim's pure-ASCII body — which is how the first attempt at
    the fix above took out every install test on Windows."""
    launcher = _version(tmp_path, "1.0.0") / "bin" / "selly-agent"
    shim = pointer.write_shim(tmp_path / "bin" / "selly-agent", launcher, "/usr/bin/python3")
    assert pointer.shim_target(shim) == launcher


def test_the_shim_template_stays_ascii() -> None:
    """It is written in the console code page, which is cp437 or cp850 on a Western install and
    has no em dash. One in a `rem` line is enough to make every install on Windows fail."""
    body = pointer._CMD_SHIM.format(target="C:/x/selly-agent", interpreter="C:/py/python.exe")
    assert body.isascii(), [c for c in body if not c.isascii()]
