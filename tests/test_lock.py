"""Instance lock: acquire, live-duplicate refusal, dead-holder reclaim, own-holder clear."""

from __future__ import annotations

import os

from sellee import lock


def test_acquire_on_fresh_path(tmp_path) -> None:
    result = lock.acquire(tmp_path / "daemon.lock")
    try:
        assert result.acquired
        assert result.holder_pid == os.getpid()
        assert result.holder_alive
        assert not result.reclaimed
        assert result.fd is not None
        assert lock.read_holder_pid(tmp_path / "daemon.lock") == os.getpid()
    finally:
        os.close(result.fd)


def test_live_duplicate_is_refused_with_truthful_holder(tmp_path) -> None:
    first = lock.acquire(tmp_path / "daemon.lock")
    try:
        second = lock.acquire(tmp_path / "daemon.lock")
        assert not second.acquired
        assert second.fd is None
        assert second.holder_pid == os.getpid()
        assert second.holder_alive
    finally:
        os.close(first.fd)


def test_dead_holder_lock_is_reclaimed(tmp_path) -> None:
    lock_path = tmp_path / "daemon.lock"
    dead_pid = 2**31 - 1  # above any real pid; is_pid_alive returns False
    assert not lock.is_pid_alive(dead_pid)
    lock_path.write_text(str(dead_pid))

    result = lock.acquire(lock_path)
    try:
        assert result.acquired
        assert result.reclaimed
        assert lock.read_holder_pid(lock_path) == os.getpid()
    finally:
        os.close(result.fd)


def test_clear_holder_only_clears_our_own(tmp_path) -> None:
    lock_path = tmp_path / "daemon.lock"

    # a foreign holder is left untouched
    lock_path.write_text("4242")
    assert lock.clear_holder(lock_path) is False
    assert lock.read_holder_pid(lock_path) == 4242

    # our own holder is cleared to an empty body
    result = lock.acquire(lock_path)
    try:
        assert lock.clear_holder(lock_path) is True
        assert lock.read_holder_pid(lock_path) is None
    finally:
        os.close(result.fd)


def test_is_pid_alive(tmp_path) -> None:
    assert lock.is_pid_alive(os.getpid())
    assert not lock.is_pid_alive(2**31 - 1)
    assert not lock.is_pid_alive(0)
    assert not lock.is_pid_alive(None)
