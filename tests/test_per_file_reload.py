"""Per-file reload: re-read one store, never the others.

``Reload all`` re-reads every store; ``Reload file`` must touch only the file
the current tab shows, ask before discarding that file's edit, and leave every
other dirty file untouched. These drive the real handlers.

The cases that matter beyond the happy path are the ones where two files are
dirty at once: a per-file reload must not notice, discard, or re-baseline the
other one, and switching files afterwards must not become a reload.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "minimal")

from qtpy.QtWidgets import QApplication, QMessageBox  # noqa: E402

from maki_trust_editor import paths, permissions, safe_write, workspace  # noqa: E402
from maki_trust_editor.window import (  # noqa: E402
    DIRTY_MARK,
    GLOBAL_TAB,
    PROJECTS_TAB,
    SESSIONS_TAB,
    MainWindow,
)

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX flock probe only")

CANCEL = QMessageBox.StandardButton.Cancel
YES = QMessageBox.StandardButton.Yes
PROJECT_EXTRA = 'default = "allow"\n[bash]\nallow = ["git *"]\n'
NEW_SCOPE = "make *"


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
    (config / "permissions.toml").write_text('default = "allow"\n', encoding="utf-8")
    (state / "trusted-folders.json").write_text(
        json.dumps({"version": 2, "folders": [], "rejected": []}), encoding="utf-8"
    )
    monkeypatch.setattr(
        safe_write, "detect_maki", lambda: safe_write.MakiStatus(running=False, pids=[])
    )
    return state, config


@pytest.fixture
def two_window(app: QApplication, tmp_path: Path, monkeypatch) -> MainWindow:
    """Two sessions and two projects, every store clean at startup."""
    state, _ = _make_state(tmp_path, monkeypatch)
    _write_session(state / "sessions" / "a.jsonl", "a", [{"tool": "bash", "effect": "allow"}])
    _write_session(state / "sessions" / "b.jsonl", "b", [{"tool": "read", "effect": "allow"}])
    files = []
    for index in range(2):
        root = tmp_path / f"proj{index}"
        (root / ".maki").mkdir(parents=True)
        (root / ".git").mkdir()
        path = root / ".maki" / "permissions.toml"
        path.write_text(PROJECT_EXTRA, encoding="utf-8")
        files.append(path)
    window = MainWindow()
    window._liveness_timer.stop()
    window._loading = True
    window.workspace.projects = [
        workspace.ProjectView(
            root=path.parent.parent,
            permissions_file=permissions.PermissionsFile.load(path),
            exists=True,
        )
        for path in files
    ]
    window._project_labels = [view.label for view in window.workspace.projects]
    window.projects_selector.clear()
    for label in window._project_labels:
        window.projects_selector.addItem(label)
    window._loading = False
    window.projects_selector.setCurrentRow(0)
    window._capture_baselines()
    return window


def _row_of(window: MainWindow, sid: str) -> int:
    for row, view in enumerate(window.workspace.sessions):
        if view.session.session_id == sid:
            return row
    raise AssertionError(sid)


def _prompt(monkeypatch, answer) -> list[list]:
    calls: list[list] = []

    def _question(*args, **_kwargs):
        calls.append(list(args))
        return answer

    monkeypatch.setattr(QMessageBox, "question", staticmethod(_question))
    return calls


def _never_prompt(monkeypatch) -> None:
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: pytest.fail("unexpected prompt"))
    )


def _edit_project(window: MainWindow) -> None:
    """Add a scope row in the project editor and park it in the model."""
    _tool, editor, _is_mcp = window.projects_tab._editors[0]
    editor.add_row(NEW_SCOPE, "deny")
    window._mark_dirty()


def _session_dirty(window: MainWindow, sid: str) -> bool:
    return _row_of(window, sid) in window._session_targets()


def test_reload_file_without_edits_does_not_prompt(two_window, monkeypatch) -> None:
    """A clean file reloads silently."""
    _never_prompt(monkeypatch)
    two_window.tabs.setCurrentIndex(SESSIONS_TAB)

    two_window._reload_current()

    assert two_window._session_targets() == {}


def test_reload_file_discards_only_the_shown_session_edit(two_window, monkeypatch) -> None:
    """Cancelling keeps the edit; accepting drops it, and nothing else moves."""
    window = two_window
    window.tabs.setCurrentIndex(SESSIONS_TAB)
    first, other = _row_of(window, "a"), _row_of(window, "b")
    window.sessions_tab.list.setCurrentRow(first)
    window.sessions_tab._editors[0][1].add_row(NEW_SCOPE, "deny")
    window.sessions_tab.list.setCurrentRow(other)
    window.sessions_tab._editors[0][1].add_row(NEW_SCOPE, "deny")
    assert _session_dirty(window, "a") and _session_dirty(window, "b")

    window.sessions_tab.list.setCurrentRow(first)
    calls = _prompt(monkeypatch, CANCEL)
    window._reload_current()
    assert calls, "reloading a dirty file must ask"
    assert _session_dirty(window, "a"), "cancel must keep the edit"

    _prompt(monkeypatch, YES)
    window._reload_current()
    assert not _session_dirty(window, "a"), "accept must drop the shown edit"
    assert _session_dirty(window, "b"), "another session's edit must survive"


def test_reload_file_names_the_dirty_file(two_window, monkeypatch) -> None:
    """The warning names the file at risk, rather than a bare 'unsaved changes'."""
    window = two_window
    window.tabs.setCurrentIndex(SESSIONS_TAB)
    row = _row_of(window, "a")
    window.sessions_tab.list.setCurrentRow(row)
    window.sessions_tab._editors[0][1].add_row(NEW_SCOPE, "deny")

    calls = _prompt(monkeypatch, CANCEL)
    window._reload_current()

    body = calls[0][2]
    assert "a.jsonl" in body
    assert "b.jsonl" not in body


def test_reload_file_keeps_a_clean_file_clean_after_switching(two_window, monkeypatch) -> None:
    """Switching files is not a reload: neither mark nor prompt appears."""
    window = two_window
    window.tabs.setCurrentIndex(SESSIONS_TAB)
    first, other = _row_of(window, "a"), _row_of(window, "b")

    window.sessions_tab.list.setCurrentRow(first)
    window._reload_current()
    window.sessions_tab.list.setCurrentRow(other)
    _never_prompt(monkeypatch)
    window._reload_current()

    assert window._session_targets() == {}
    for row in (first, other):
        assert window.sessions_tab.list.item(row).text() == window.sessions_tab.row_label(row)


def test_reload_file_of_a_project_leaves_dirty_sessions_alone(two_window, monkeypatch) -> None:
    """A project reload must not touch a dirty session on another tab."""
    window = two_window
    window.tabs.setCurrentIndex(SESSIONS_TAB)
    row = _row_of(window, "a")
    window.sessions_tab.list.setCurrentRow(row)
    window.sessions_tab._editors[0][1].add_row(NEW_SCOPE, "deny")
    marked = window.sessions_tab.list.item(row).text()
    assert marked.endswith(DIRTY_MARK)

    window.tabs.setCurrentIndex(PROJECTS_TAB)
    window.projects_selector.setCurrentRow(0)
    window.tabs.setCurrentIndex(PROJECTS_TAB)
    _never_prompt(monkeypatch)
    window._reload_current()

    assert _session_dirty(window, "a")
    assert window.sessions_tab.list.item(row).text() == marked


def test_reload_file_of_a_project_discards_its_own_edit(two_window, monkeypatch) -> None:
    """A project's own edit is what a project reload asks about and drops."""
    window = two_window
    _never_prompt(monkeypatch)
    window.tabs.setCurrentIndex(PROJECTS_TAB)
    window.projects_selector.setCurrentRow(1)

    window._reload_current()

    assert not window._project_targets()
    assert NEW_SCOPE not in window.workspace.projects[0].permissions_file.tools["bash"].allow


def test_reload_file_after_switching_projects_reloads_the_shown_one(
    two_window, monkeypatch
) -> None:
    """The reload follows the selection, not the project that was open at load."""
    window = two_window
    _never_prompt(monkeypatch)
    window.tabs.setCurrentIndex(PROJECTS_TAB)
    window.projects_selector.setCurrentRow(1)

    window._reload_current()

    assert not window._project_targets()


def test_reload_all_prompts_once_for_every_dirty_file(two_window, monkeypatch) -> None:
    """Reload all warns once and names each dirty file."""
    window = two_window
    window.tabs.setCurrentIndex(SESSIONS_TAB)
    row = _row_of(window, "a")
    window.sessions_tab.list.setCurrentRow(row)
    window.sessions_tab._editors[0][1].add_row(NEW_SCOPE, "deny")
    window.tabs.setCurrentIndex(GLOBAL_TAB)
    window.workspace.global_permissions.default = "deny"
    window._mark_dirty()

    calls = _prompt(monkeypatch, CANCEL)
    window._reload()

    assert len(calls) == 1
    body = calls[0][2]
    assert "a.jsonl" in body and "permissions.toml" in body
    assert _session_dirty(window, "a"), "cancel must keep every edit"
