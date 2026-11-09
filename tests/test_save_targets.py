"""Save must touch only the stores the user actually changed.

This drives ``MainWindow._collect_targets`` against a throwaway workspace and
asserts that an untouched store is never collected, so the bug where editing one
session rewrote (and re-backed-up) every file cannot come back.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "minimal")

from qtpy.QtWidgets import QApplication  # noqa: E402

from maki_trust_editor import paths  # noqa: E402
from maki_trust_editor.window import DIRTY_MARK, TAB_TITLES, MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _write_session(path: Path, rules: list[dict]) -> None:
    header = {"t": "header", "id": "s1", "cwd": "/p", "model": "m"}
    meta = {"t": "meta", "title": "t", "session_rules": rules}
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
def window(app: QApplication, tmp_path: Path, monkeypatch) -> MainWindow:
    state, config = _make_state(tmp_path, monkeypatch)
    _write_session(state / "sessions" / "s1.jsonl", [{"tool": "bash", "effect": "allow"}])
    (config / "permissions.toml").write_text('default = "allow"\n', encoding="utf-8")
    (state / "trusted-folders.json").write_text(
        json.dumps({"version": 2, "folders": [], "rejected": []}), encoding="utf-8"
    )
    return MainWindow()


def _paths_of(targets) -> set[Path]:
    return {target.path for target in targets}


def _edit_global(window: MainWindow, default: str = "deny") -> None:
    """Mark the global tab dirty by flipping its default and dropping editors."""
    window.global_tab._file.default = default
    window.global_tab.scoped.clear_editors()
    window.global_tab._editors.clear()


def test_no_edits_collects_no_targets(window: MainWindow) -> None:
    assert window._collect_targets() == []
    assert window._dirty() is False


def test_dirty_clears_after_reverting_an_edit(window: MainWindow) -> None:
    _edit_global(window)
    assert window._dirty() is True

    window.global_tab._file.default = "allow"
    assert window._dirty() is False


def test_tab_titles_are_clean_after_load(window: MainWindow) -> None:
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert titles == list(TAB_TITLES)


def test_edited_tab_title_gets_dirty_mark(window: MainWindow) -> None:
    _edit_global(window)

    window._refresh_marks()

    assert window.tabs.tabText(3) == "Global" + DIRTY_MARK
    assert window.tabs.tabText(0) == "Sessions"


def test_dirty_mark_clears_after_save(window: MainWindow, monkeypatch) -> None:
    from maki_trust_editor import safe_write

    monkeypatch.setattr(
        safe_write, "detect_maki", lambda: safe_write.MakiStatus(running=False, pids=[])
    )
    _edit_global(window)
    window._mark_dirty()
    assert window.tabs.tabText(3) == "Global" + DIRTY_MARK

    window.tabs.setCurrentIndex(3)
    window._save()

    assert window.tabs.tabText(3) == "Global"


def _edit_session(window: MainWindow) -> None:
    window.sessions_tab.list.setCurrentRow(0)
    window.sessions_tab._editors[0][1].add_row("git *", "deny")


def _edit_pre_trust(window: MainWindow) -> None:
    window.folders_tab._pre_trust.add("/some/root")


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (_edit_session, lambda w: {w.workspace.sessions[0].session.path}),
        (_edit_global, lambda w: {paths.global_permissions_path()}),
        (_edit_pre_trust, lambda w: {w.workspace.pre_trust_roots.path}),
    ],
    ids=["session", "global", "pre_trust"],
)
def test_edit_collects_only_that_store(window: MainWindow, mutate, expected) -> None:
    mutate(window)

    assert _paths_of(window._collect_targets()) == expected(window)


def _drive_add_group(window: MainWindow, name: str, monkeypatch) -> list[str]:
    """Run ``_add_group`` with a scripted name; return any warnings shown."""
    from qtpy.QtWidgets import QInputDialog, QMessageBox

    warnings: list[str] = []
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: (name, True))
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *a, **k: warnings.append(a[2] if len(a) > 2 else "")
    )
    window.sessions_tab.list.setCurrentRow(0)
    window.sessions_tab._add_group()
    return warnings


def test_add_group_rejects_bad_names(window: MainWindow, monkeypatch) -> None:
    window.sessions_tab.list.setCurrentRow(0)

    for name in ("bash", "bad name"):
        warnings = _drive_add_group(window, name, monkeypatch)
        assert warnings, name
        assert [tool for tool, _ in window.sessions_tab._editors] == ["bash"], name


def test_empty_session_file_is_not_dirty(app, tmp_path: Path, monkeypatch) -> None:
    """A session with no meta line has no baseline, so it is never 'changed'."""
    state, config = _make_state(tmp_path, monkeypatch)
    (state / "sessions" / "s1.jsonl").write_text("", encoding="utf-8")
    (config / "permissions.toml").write_text('default = "allow"\n', encoding="utf-8")

    window = MainWindow()

    assert window._collect_targets() == []
    assert window._dirty() is False


def test_missing_project_file_is_not_dirty(app, tmp_path: Path, monkeypatch) -> None:
    """A project whose permissions.toml does not exist is not spuriously dirty."""
    state, config = _make_state(tmp_path, monkeypatch)
    _write_session(state / "sessions" / "s1.jsonl", [])
    (config / "permissions.toml").write_text('default = "allow"\n', encoding="utf-8")

    window = MainWindow()

    assert window._dirty() is False


def test_opening_session_with_unsorted_rules_is_not_dirty(app, tmp_path: Path, monkeypatch) -> None:
    """Rules stored in a non-canonical order must not read as an edit.

    The editor rebuilds rules grouped by tool and sorted, so the baseline has
    to be the same canonical rendering; otherwise merely opening such a session
    raises the unsaved-changes asterisk.
    """
    state, config = _make_state(tmp_path, monkeypatch)
    _write_session(
        state / "sessions" / "s1.jsonl",
        [
            {"tool": "bash", "scope": "git *", "effect": "deny"},
            {"tool": "bash", "effect": "allow"},
        ],
    )
    (config / "permissions.toml").write_text('default = "allow"\n', encoding="utf-8")

    window = MainWindow()
    window.sessions_tab.list.setCurrentRow(0)

    assert window._dirty() is False

    window.sessions_tab._editors[0][1].add_row("ls *", "allow")
    assert window._dirty() is True


def test_no_edits_switches_projects_without_prompt(app, tmp_path, monkeypatch) -> None:
    state, config = _make_state(tmp_path, monkeypatch)
    _write_session(state / "sessions" / "s1.jsonl", [])
    (config / "permissions.toml").write_text('default = "allow"\n', encoding="utf-8")
    window = MainWindow()

    from qtpy.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: pytest.fail("should not prompt"))

    window.projects_selector.setCurrentRow(0)

    assert window._last_project_row == 0


MALFORMED_CASES = {
    "toml_global": (paths.global_permissions_path, "bad = = 1\n"),
    "toml_unclosed": (paths.global_permissions_path, '[bash\nallow = ["x"]\n'),
    "json_trust_garbage": (paths.trusted_folders_path, "{not json}"),
    "json_trust_truncated": (paths.trusted_folders_path, '{"version": 2,'),
    "json_pre_trust_not_a_list": (paths.pre_trust_roots_path, "{oops}"),
}


@pytest.mark.parametrize("case", MALFORMED_CASES)
def test_malformed_store_loads_empty_and_flashes(
    app, tmp_path: Path, monkeypatch, case: str
) -> None:
    from qtpy.QtWidgets import QMessageBox

    path_fn, content = MALFORMED_CASES[case]
    _make_state(tmp_path, monkeypatch)
    target = path_fn()
    target.write_text(content, encoding="utf-8")

    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[2]))

    window = MainWindow()

    assert [p.path for p in window.workspace.problems] == [target]
    assert shown and str(target) in shown[0]
