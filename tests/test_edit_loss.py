"""Unsaved edits must survive tab switches, selector switches and Reload.

Three paths used to drop an edit before it could be saved:

* ``_tab_changed`` re-read Global and then recaptured *every* store's baseline,
  so switching to Global cleared another store's dirty mark and its edit was
  never written.
* ``_project_selected`` reloaded the incoming project unconditionally, and
  ``_commit_tab`` parked the visible editor in ``projects_selector.currentRow()``
  (already the incoming row), so a project round-trip lost the edit.
* ``_reload`` re-read everything with no prompt, unlike Close.

Each test drives the real handler and asserts the edit is still there.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "minimal")

from qtpy.QtWidgets import QApplication, QMessageBox  # noqa: E402

from maki_trust_editor import paths, permissions, workspace  # noqa: E402
from maki_trust_editor.window import GLOBAL_TAB, MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def no_prompt(monkeypatch) -> None:
    """Any dialog that fires fails the test: these paths must not prompt."""
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: pytest.fail("unexpected prompt"))
    )


def _make_projects(tmp_path: Path, monkeypatch, bodies: list[str]) -> list[Path]:
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

    files: list[Path] = []
    for index, body in enumerate(bodies):
        root = tmp_path / f"proj{index}"
        (root / ".maki").mkdir(parents=True)
        (root / ".git").mkdir()
        path = root / ".maki" / "permissions.toml"
        path.write_text(body, encoding="utf-8")
        files.append(path)
    return files


def _install_projects(window: MainWindow, tmp_path: Path, files: list[Path]) -> None:
    """Point the window at the given project files, as if discovered on load."""
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


PROJECT_BODY = 'default = "allow"\n[bash]\nallow = ["git *"]\n'


def test_edit_survives_switch_to_global_tab(app, tmp_path, monkeypatch, no_prompt) -> None:
    """F1: visiting Global must not clear another store's dirty mark."""
    files = _make_projects(tmp_path, monkeypatch, [PROJECT_BODY])
    window = MainWindow()
    _install_projects(window, tmp_path, files)
    window.workspace.projects[0].permissions_file.tools["bash"].deny = ["rm *"]
    assert window._project_targets()  # dirty before the switch

    window._tab_changed(GLOBAL_TAB)

    assert window._project_targets(), "switching to Global cleared the project edit"
    assert window.workspace.projects[0].permissions_file.tools["bash"].deny == ["rm *"]


def test_edit_survives_project_selector_round_trip(app, tmp_path, monkeypatch, no_prompt) -> None:
    """F2: the visible editor must be parked in the project it shows."""
    files = _make_projects(
        tmp_path, monkeypatch, [PROJECT_BODY, 'default = "deny"\n[read]\nallow = ["src/*"]\n']
    )
    window = MainWindow()
    _install_projects(window, tmp_path, files)
    editor = next(
        e for name, e, is_mcp in window.projects_tab._editors if name == "bash" and not is_mcp
    )
    editor.add_row("rm *", "deny")

    window.projects_selector.setCurrentRow(1)
    window.projects_selector.setCurrentRow(0)

    p0 = window.workspace.projects[0].permissions_file
    p1 = window.workspace.projects[1].permissions_file
    assert p0.tools["bash"].deny == ["rm *"], "project edit lost by selector round-trip"
    assert p0.tools["bash"].allow == ["git *"]
    assert sorted(p1.tools) == ["read"], "p0's content leaked into p1"


def test_reload_prompts_before_discarding_an_edit(app, tmp_path, monkeypatch) -> None:
    """F3: Reload must confirm before it drops an unsaved edit."""
    _make_projects(tmp_path, monkeypatch, [PROJECT_BODY])
    window = MainWindow()
    window.workspace.global_permissions.default = "deny"
    assert window._dirty()

    prompts: list[int] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: prompts.append(1) or QMessageBox.StandardButton.Cancel),
    )
    window._reload()

    assert prompts == [1], "Reload discarded an unsaved edit without asking"


def test_reload_without_edits_does_not_prompt(app, tmp_path, monkeypatch, no_prompt) -> None:
    """Near-miss: a clean window reloads silently."""
    _make_projects(tmp_path, monkeypatch, [PROJECT_BODY])
    window = MainWindow()

    window._reload()
