"""The live refresh must notice a lock appearing or vanishing.

A running maki opens or closes a session with nothing in this process touched,
so the view used to lie until Save. ``_refresh_liveness`` re-probes the visible
rows and flips the read-only state; these tests call it directly (no timer, no
sleeps) and assert the flip, the park of a dirty edit, and the banner.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "minimal")

from qtpy.QtWidgets import QApplication  # noqa: E402

from maki_trust_editor import paths, safe_write, sessions  # noqa: E402
from maki_trust_editor.window import DIRTY_MARK, MainWindow  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX flock probe only")

HELD_PID = 4242


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _write_session(path: Path, session_id: str, rules: list[dict] | None = None) -> None:
    header = {"t": "header", "id": session_id, "cwd": "/p", "model": "m"}
    meta = {
        "t": "meta",
        "title": "t",
        "session_rules": rules or [{"tool": "bash", "effect": "allow"}],
    }
    path.write_text("\n".join(json.dumps(x) for x in (header, meta)) + "\n", encoding="utf-8")


def _make_state(tmp_path: Path, monkeypatch, ids: list[str]) -> Path:
    state = tmp_path / "state"
    config = tmp_path / "config"
    (state / "sessions" / sessions.LOCKS_DIR_NAME).mkdir(parents=True)
    config.mkdir()
    monkeypatch.setattr(paths, "sessions_dir", lambda: state / "sessions")
    monkeypatch.setattr(paths, "state_dir", lambda: state)
    monkeypatch.setattr(paths, "config_dir", lambda: config)
    monkeypatch.setattr(paths, "trusted_folders_path", lambda: state / "trusted-folders.json")
    monkeypatch.setattr(
        paths, "pre_trust_roots_path", lambda: state / "sessions" / "pre-trust-roots.json"
    )
    monkeypatch.setattr(paths, "global_permissions_path", lambda: config / "permissions.toml")
    for session_id in ids:
        _write_session(state / "sessions" / f"{session_id}.jsonl", session_id)
    (config / "permissions.toml").write_text('default = "allow"\n', encoding="utf-8")
    (state / "trusted-folders.json").write_text(
        json.dumps({"version": 2, "folders": [], "rejected": []}), encoding="utf-8"
    )
    return state / "sessions"


def _hold_lock(sessions_dir: Path, session_id: str):
    """Take the exclusive lock a running maki would hold, and return its handle."""
    path = sessions_dir / sessions.LOCKS_DIR_NAME / session_id
    path.write_bytes(b"")
    handle = open(path, "rb")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def _no_maki(monkeypatch) -> None:
    """Freeze the process probe so the tick never announces a real maki."""
    monkeypatch.setattr(
        safe_write, "detect_maki", lambda: safe_write.MakiStatus(running=False, pids=[])
    )


def test_lock_appearing_flips_the_active_row_read_only(app, tmp_path, monkeypatch) -> None:
    """A session maki opens mid-view becomes read-only within one tick."""
    sdir = _make_state(tmp_path, monkeypatch, ["s1"])
    _no_maki(monkeypatch)
    window = MainWindow()
    window.sessions_tab.list.setCurrentRow(0)
    assert window.sessions_tab.scoped.is_read_only() is False

    handle = _hold_lock(sdir, "s1")
    try:
        window._refresh_liveness()
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    assert window.workspace.sessions[0].locked is True
    assert window.sessions_tab.scoped.is_read_only() is True
    assert window.sessions_tab._add_button.isEnabled() is False


def test_lock_vanishing_re_enables_the_active_row(app, tmp_path, monkeypatch) -> None:
    """A session maki closes reopens for editing within one tick."""
    sdir = _make_state(tmp_path, monkeypatch, ["s1"])
    _no_maki(monkeypatch)
    handle = _hold_lock(sdir, "s1")
    try:
        window = MainWindow()
        window.sessions_tab.list.setCurrentRow(0)
        assert window.sessions_tab.scoped.is_read_only() is True

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        window._refresh_liveness()
    finally:
        handle.close()

    assert window.workspace.sessions[0].locked is False
    assert window.sessions_tab.scoped.is_read_only() is False
    assert window.sessions_tab._add_button.isEnabled() is True


def test_lock_appearing_parks_a_dirty_edit(app, tmp_path, monkeypatch) -> None:
    """A lock arriving over an unsaved edit keeps the edit, drops the row from
    the save targets, and warns on the status line."""
    sdir = _make_state(tmp_path, monkeypatch, ["s1"])
    _no_maki(monkeypatch)
    window = MainWindow()
    window.sessions_tab.list.setCurrentRow(0)
    editor = window.sessions_tab._editors[0][1]
    editor.add_row("rm *", "deny")
    window.sessions_tab.commit()
    assert window._session_targets(), "edit was not dirty before the lock"

    handle = _hold_lock(sdir, "s1")
    try:
        window._refresh_liveness()
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    assert window.workspace.sessions[0].locked is True
    assert window._session_targets() == {}, "a locked session must not be a save target"
    parked = window.workspace.sessions[0].session.rules
    assert any(
        r.tool == "bash" and r.scope == "rm *" and r.effect == "deny" for r in parked
    ), "edit dropped"
    assert "parked" in window.status.text()


def test_unlock_after_maki_rewrites_is_not_dirty(app, tmp_path, monkeypatch) -> None:
    """maki rewriting a session while it holds it must not read as an edit.

    A regression: the baseline was taken at load, so the content reloaded on
    *unlock* (maki's own rewrite) differed from it and the row showed unsaved
    changes the user never made.
    """
    sdir = _make_state(tmp_path, monkeypatch, ["s1"])
    _no_maki(monkeypatch)
    window = MainWindow()
    window.sessions_tab.list.setCurrentRow(0)
    assert not window._session_targets()

    handle = _hold_lock(sdir, "s1")
    try:
        _write_session(
            sdir / "s1.jsonl",
            "s1",
            rules=[{"tool": "bash", "effect": "allow"}, {"tool": "read", "effect": "deny"}],
        )
        window._refresh_liveness()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        window._refresh_liveness()
    finally:
        handle.close()

    assert window.workspace.sessions[0].locked is False
    assert window._session_targets() == {}, "unlocking a session maki rewrote marked it dirty"
    assert DIRTY_MARK not in window.sessions_tab.list.item(0).text()


def test_parked_edit_survives_unlock_and_stays_dirty(app, tmp_path, monkeypatch) -> None:
    """A parked edit is not re-baselined away when the session unlocks.

    Unlock must adopt the *file's* content as baseline only when there is no
    pending edit; a parked edit stays dirty so the user can still save it.
    """
    sdir = _make_state(tmp_path, monkeypatch, ["s1"])
    _no_maki(monkeypatch)
    window = MainWindow()
    window.sessions_tab.list.setCurrentRow(0)
    editor = window.sessions_tab._editors[0][1]
    editor.add_row("rm *", "deny")
    window.sessions_tab.commit()

    handle = _hold_lock(sdir, "s1")
    try:
        window._refresh_liveness()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        window._refresh_liveness()
    finally:
        handle.close()

    assert window.workspace.sessions[0].locked is False
    parked = window.workspace.sessions[0].session.rules
    assert any(
        r.scope == "rm *" and r.effect == "deny" for r in parked
    ), "parked edit dropped by unlock"
    assert window._session_targets(), "parked edit must be dirty again after unlock"
    assert DIRTY_MARK in window.sessions_tab.list.item(0).text()


def test_tick_announces_maki_start_and_exit(app, tmp_path, monkeypatch) -> None:
    """The tick reports the process fact changing, once per transition."""
    _make_state(tmp_path, monkeypatch, ["s1"])
    running = {"value": False}
    monkeypatch.setattr(
        safe_write,
        "detect_maki",
        lambda: safe_write.MakiStatus(running=running["value"], pids=[HELD_PID]),
    )
    window = MainWindow()
    window._refresh_liveness()  # first tick records the baseline, no message

    running["value"] = True
    window._refresh_liveness()
    assert "maki started" in window.status.text()

    window._refresh_liveness()  # still running: no new message
    assert "maki exited" not in window.status.text()

    running["value"] = False
    window._refresh_liveness()
    assert "maki exited" in window.status.text()
