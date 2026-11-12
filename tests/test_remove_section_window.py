"""Removing a section must reach the model a save is built from.

``collect()`` rebuilds the file from the tab's editor list, so a section that is
gone from the widget but still in the list would be written straight back. These
drive the real window: the section leaves the list, the saved file no longer
contains it, and a populated section asks before going.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "minimal")

from qtpy.QtWidgets import QApplication, QMessageBox  # noqa: E402

from maki_trust_editor import paths, permissions, safe_write, sessions, workspace  # noqa: E402
from maki_trust_editor.window import MainWindow  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX flock probe only")

CANCEL = QMessageBox.StandardButton.Cancel
YES = QMessageBox.StandardButton.Yes
PROJECT_EXTRA = 'default = "allow"\n[bash]\nallow = ["git *"]\n[read]\nallow = ["/etc/**"]\n'


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _make_state(tmp_path: Path, monkeypatch) -> Path:
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
    return state


def _session(path: Path, sid: str, rules: list[dict]) -> None:
    header = {"t": "header", "id": sid, "cwd": "/p", "model": "m"}
    meta = {"t": "meta", "title": sid, "session_rules": rules}
    path.write_text("\n".join(json.dumps(x) for x in (header, meta)) + "\n", encoding="utf-8")


@pytest.fixture
def window(app: QApplication, tmp_path: Path, monkeypatch) -> MainWindow:
    state = _make_state(tmp_path, monkeypatch)
    _session(
        state / "sessions" / "a.jsonl",
        "a",
        [
            {"tool": "bash", "effect": "allow", "scope": "git *"},
            {"tool": "read", "effect": "allow"},
        ],
    )
    project = tmp_path / "proj"
    (project / ".maki").mkdir(parents=True)
    (project / ".git").mkdir()
    path = project / ".maki" / "permissions.toml"
    path.write_text(PROJECT_EXTRA, encoding="utf-8")

    win = MainWindow()
    win._liveness_timer.stop()
    win._loading = True
    win.workspace.sessions = [
        workspace.SessionView(session=sessions.Session.load(state / "sessions" / "a.jsonl"))
    ]
    win.workspace.projects = [
        workspace.ProjectView(
            root=project,
            permissions_file=permissions.PermissionsFile.load(path),
            exists=True,
        )
    ]
    win._project_labels = [view.label for view in win.workspace.projects]
    win.projects_selector.clear()
    for label in win._project_labels:
        win.projects_selector.addItem(label)
    win._loading = False
    win._populate()
    win.sessions_tab.list.setCurrentRow(0)
    win.projects_selector.setCurrentRow(0)
    win._capture_baselines()
    return win


def _answer(monkeypatch, answer) -> list[int]:
    calls: list[int] = []

    def _question(*_args, **_kwargs):
        calls.append(1)
        return answer

    monkeypatch.setattr(QMessageBox, "question", staticmethod(_question))
    return calls


def _project_section(window: MainWindow, name: str):
    """The (name, editor, is_mcp) entry for one project section."""
    return next(e for e in window.projects_tab._editors if e[0] == name)


def test_removing_a_project_section_drops_it_from_the_saved_file(window, monkeypatch) -> None:
    _answer(monkeypatch, YES)
    editor = _project_section(window, "read")[1]

    window.projects_tab._remove_section(editor)
    window._mark_dirty()

    saved = window.projects_tab.collect().to_toml()
    assert "[read]" not in saved
    assert "[bash]" in saved


def test_removing_a_section_leaves_the_others_alone(window, monkeypatch) -> None:
    _answer(monkeypatch, YES)
    editor = _project_section(window, "bash")[1]

    window.projects_tab._remove_section(editor)

    remaining = [e[0] for e in window.projects_tab._editors]
    assert remaining == ["read"]
    assert window.projects_tab._file.tools.keys() == {"read"}


def test_removing_a_populated_section_asks_first(window, monkeypatch) -> None:
    calls = _answer(monkeypatch, CANCEL)
    editor = _project_section(window, "read")[1]

    window.projects_tab._remove_section(editor)

    assert calls == [1]
    assert "read" in [e[0] for e in window.projects_tab._editors]
    assert "read" in window.projects_tab._file.tools


def test_removing_an_empty_section_does_not_ask(window, monkeypatch) -> None:
    calls = _answer(monkeypatch, CANCEL)
    empty = window.projects_tab.add_tool_section("grep")
    assert empty is None
    editor = _project_section(window, "grep")[1]

    window.projects_tab._remove_section(editor)

    assert calls == []
    assert "grep" not in [e[0] for e in window.projects_tab._editors]


def test_removing_a_session_group_drops_its_rules(window, monkeypatch) -> None:
    _answer(monkeypatch, YES)
    editor = next(e[1] for e in window.sessions_tab._editors if e[0] == "bash")

    window.sessions_tab._remove_group(editor)

    tools = [tool for tool, _ in window.sessions_tab._editors]
    assert "bash" not in tools
    assert "read" in tools
    assert all(rule.tool != "bash" for rule in window.sessions_tab.collect())
