"""Per-file save and persistent dirty state.

Save writes only the current selection's file; edits survive switching away and
are shown as dirty in the list, so several sessions can be edited before any is
written. A locked session is read-only and is never saved.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "minimal")

from qtpy.QtWidgets import QApplication  # noqa: E402

from maki_trust_editor import paths, safe_write  # noqa: E402
from maki_trust_editor.window import DIRTY_MARK, MainWindow  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX flock probe only")


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _write_session(path: Path, sid: str, rules: list[dict]) -> None:
    header = {"t": "header", "id": sid, "cwd": "/p", "model": "m"}
    meta = {"t": "meta", "title": sid, "session_rules": rules}
    path.write_text("\n".join(json.dumps(x) for x in (header, meta)) + "\n", encoding="utf-8")


def _make_state(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    state = tmp_path / "state"
    config = tmp_path / "config"
    (state / "sessions").mkdir(parents=True)
    config.mkdir()
    monkeypatch.setattr(paths, "sessions_dir", lambda: state / "sessions")
    monkeypatch.setattr(paths, "state_dir", lambda: state)
    monkeypatch.setattr(paths, "config_dir", lambda: config)
    monkeypatch.setattr(paths, "trusted_folders_path", lambda: state / "trusted-folders.json")
    monkeypatch.setattr(
        paths, "pre_trust_roots_path", lambda: state / "sessions" / "pre-trust-roots.json"
    )
    monkeypatch.setattr(paths, "global_permissions_path", lambda: config / "permissions.toml")
    return state, config


@pytest.fixture
def two_sessions(app: QApplication, tmp_path: Path, monkeypatch) -> MainWindow:
    state, config = _make_state(tmp_path, monkeypatch)
    _write_session(state / "sessions" / "a.jsonl", "a", [{"tool": "bash", "effect": "allow"}])
    _write_session(state / "sessions" / "b.jsonl", "b", [{"tool": "read", "effect": "allow"}])
    (config / "permissions.toml").write_text('default = "allow"\n', encoding="utf-8")
    (state / "trusted-folders.json").write_text(
        json.dumps({"version": 2, "folders": [], "rejected": []}), encoding="utf-8"
    )
    monkeypatch.setattr(
        safe_write, "detect_maki", lambda: safe_write.MakiStatus(running=False, pids=[])
    )
    return MainWindow()


def _row_of(window: MainWindow, sid: str) -> int:
    for row, view in enumerate(window.workspace.sessions):
        if view.session.session_id == sid:
            return row
    raise AssertionError(sid)


def _row_text(window: MainWindow, row: int) -> str:
    return window.sessions_tab.list.item(row).text()


def _edit_first_group(window: MainWindow, scope: str = "git *") -> None:
    window.sessions_tab._editors[0][1].add_row(scope, "deny")


def test_opening_a_session_is_not_dirty(two_sessions: MainWindow) -> None:
    assert two_sessions._dirty() is False
    assert two_sessions.tabs.tabText(0) == "Sessions"


def test_edit_marks_tab_and_row_dirty(two_sessions: MainWindow) -> None:
    row = _row_of(two_sessions, "a")
    two_sessions.sessions_tab.list.setCurrentRow(row)
    _edit_first_group(two_sessions)

    assert two_sessions.tabs.tabText(0) == "Sessions" + DIRTY_MARK
    assert _row_text(two_sessions, row) == two_sessions.sessions_tab.row_label(row) + DIRTY_MARK


def test_edit_persists_across_switch_and_back(two_sessions: MainWindow) -> None:
    first = _row_of(two_sessions, "a")
    other = _row_of(two_sessions, "b")
    two_sessions.sessions_tab.list.setCurrentRow(first)
    _edit_first_group(two_sessions)

    two_sessions.sessions_tab.list.setCurrentRow(other)
    two_sessions.sessions_tab.list.setCurrentRow(first)

    scopes = [r.scope() for r in two_sessions.sessions_tab._editors[0][1]._rows]
    assert "git *" in scopes


def test_dirty_marks_do_not_linger_after_switch(two_sessions: MainWindow) -> None:
    first = _row_of(two_sessions, "a")
    other = _row_of(two_sessions, "b")
    two_sessions.sessions_tab.list.setCurrentRow(first)

    two_sessions.sessions_tab.list.setCurrentRow(other)

    assert _row_text(two_sessions, first) == two_sessions.sessions_tab.row_label(first)


def test_save_writes_only_the_current_session(two_sessions: MainWindow) -> None:
    state = paths.sessions_dir()
    first = _row_of(two_sessions, "a")
    other = _row_of(two_sessions, "b")
    two_sessions.sessions_tab.list.setCurrentRow(first)
    _edit_first_group(two_sessions)
    # Edit the other session too, so both are dirty.
    two_sessions.sessions_tab.list.setCurrentRow(other)
    _edit_first_group(two_sessions, "ls *")

    a_before = (state / "a.jsonl").read_text()
    two_sessions.sessions_tab.list.setCurrentRow(first)
    two_sessions._save()

    a_after = (state / "a.jsonl").read_text()
    b_after = (state / "b.jsonl").read_text()
    assert "git *" in a_after
    assert a_after != a_before
    assert "ls *" not in b_after


def test_save_clears_only_the_saved_row(two_sessions: MainWindow) -> None:
    first = _row_of(two_sessions, "a")
    other = _row_of(two_sessions, "b")
    two_sessions.sessions_tab.list.setCurrentRow(first)
    _edit_first_group(two_sessions)
    two_sessions.sessions_tab.list.setCurrentRow(other)
    _edit_first_group(two_sessions, "ls *")

    two_sessions.sessions_tab.list.setCurrentRow(first)
    two_sessions._save()

    assert _row_text(two_sessions, first) == two_sessions.sessions_tab.row_label(first)
    assert _row_text(two_sessions, other) == two_sessions.sessions_tab.row_label(other) + DIRTY_MARK


def _hold_lock(state: Path, sid: str):
    import fcntl

    locks = state / "sessions" / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    handle = open(locks / sid, "wb")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def test_locked_session_is_read_only(app, tmp_path, monkeypatch) -> None:
    state, config = _make_state(tmp_path, monkeypatch)
    _write_session(state / "sessions" / "a.jsonl", "a", [{"tool": "bash", "effect": "allow"}])
    (config / "permissions.toml").write_text('default = "allow"\n', encoding="utf-8")
    (state / "trusted-folders.json").write_text(
        json.dumps({"version": 2, "folders": [], "rejected": []}), encoding="utf-8"
    )
    handle = _hold_lock(state, "a")
    try:
        window = MainWindow()
        row = _row_of(window, "a")
        window.sessions_tab.list.setCurrentRow(row)

        editor = window.sessions_tab._editors[0][1]
        assert editor._add.isEnabled() is False
        assert editor.default_box.isEnabled() is False
        assert window.sessions_tab._add_button.isEnabled() is False
    finally:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def test_locked_session_is_not_saved(app, tmp_path, monkeypatch) -> None:
    state, config = _make_state(tmp_path, monkeypatch)
    _write_session(state / "sessions" / "a.jsonl", "a", [{"tool": "bash", "effect": "allow"}])
    (config / "permissions.toml").write_text('default = "allow"\n', encoding="utf-8")
    (state / "trusted-folders.json").write_text(
        json.dumps({"version": 2, "folders": [], "rejected": []}), encoding="utf-8"
    )
    # The session is unlocked while edited, then locked before the save.
    window = MainWindow()
    row = _row_of(window, "a")
    window.sessions_tab.list.setCurrentRow(row)
    _edit_first_group(window)

    handle = _hold_lock(state, "a")
    try:
        before = (state / "sessions" / "a.jsonl").read_text()
        monkeypatch.setattr(
            safe_write, "detect_maki", lambda: safe_write.MakiStatus(running=False, pids=[])
        )
        monkeypatch.setattr(
            "maki_trust_editor.window.QMessageBox.information", lambda *a, **k: None
        )
        window._save()
        assert (state / "sessions" / "a.jsonl").read_text() == before
    finally:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
