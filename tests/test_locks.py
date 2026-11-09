"""Session lock detection.

A running maki holds an advisory exclusive lock on ``sessions/locks/<id>``; our
probe takes a non-blocking shared lock and reads Held as "open". These tests
create the lock with the same POSIX primitive maki uses.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from maki_trust_editor import sessions

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX flock probe only")


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    locks = tmp_path / sessions.LOCKS_DIR_NAME
    locks.mkdir()
    return locks


def test_absent_lock_file_is_unlocked(tmp_path: Path, lock_dir: Path) -> None:
    assert sessions.is_locked(tmp_path, "missing") is False


def test_unheld_lock_file_is_unlocked(tmp_path: Path, lock_dir: Path) -> None:
    (lock_dir / "sess-1").write_bytes(b"")
    assert sessions.is_locked(tmp_path, "sess-1") is False


def test_held_lock_file_is_locked(tmp_path: Path, lock_dir: Path) -> None:
    import fcntl

    (lock_dir / "sess-2").write_bytes(b"")
    handle = open(lock_dir / "sess-2", "rb")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert sessions.is_locked(tmp_path, "sess-2") is True
        # A different id in the same dir stays unlocked.
        assert sessions.is_locked(tmp_path, "sess-1") is False
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    assert sessions.is_locked(tmp_path, "sess-2") is False
