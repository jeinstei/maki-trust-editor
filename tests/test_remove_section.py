"""Removing a section must delete it, not hide it.

The section box, its editor and its button are destroyed outright, the picker
loses the name, and a section that still holds rules asks first. Hiding would
leave the widgets alive and still collectable on save.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "minimal")

from qtpy.QtCore import QCoreApplication, QEvent  # noqa: E402
from qtpy.QtWidgets import QApplication, QPushButton  # noqa: E402

from maki_trust_editor.widgets import (  # noqa: E402
    REMOVE_SECTION_BUTTON,
    ScopedHolder,
    ScopeRulesEditor,
)


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _flush_deletes() -> None:
    """Run the deferred deletes ``deleteLater`` queued."""
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _is_destroyed(obj) -> bool:
    """A deleted C++ object raises on any attribute access."""
    try:
        obj.objectName()
        return False
    except RuntimeError:
        return True


class Harness:
    """A holder wired the way the tabs wire it: removal drops its own list."""

    def __init__(self) -> None:
        self.holder = ScopedHolder("Rules", "add something")
        self.editors: list[ScopeRulesEditor] = []

    def add(self, label: str, tool: str, rows: list[tuple[str, str]]) -> ScopeRulesEditor:
        editor = ScopeRulesEditor(lambda: None, tool)
        for scope, effect in rows:
            editor.add_row(scope, effect)
        self.holder.add_editor(label, editor, self.remove)
        self.editors.append(editor)
        return editor

    def remove(self, editor: ScopeRulesEditor) -> None:
        self.editors.remove(editor)
        self.holder.remove_editor(editor)

    def section(self, editor: ScopeRulesEditor) -> tuple:
        return next(s for s in self.holder._sections if s[2] is editor)

    def button(self, editor: ScopeRulesEditor) -> QPushButton:
        return self.section(editor)[1].findChild(QPushButton, REMOVE_SECTION_BUTTON)


def test_remove_button_destroys_the_section(app: QApplication) -> None:
    harness = Harness()
    kept = harness.add("[read]", "read", [("a", "allow")])
    doomed = harness.add("[bash]", "bash", [("git *", "allow")])
    box = harness.section(doomed)[1]
    button = harness.button(doomed)

    button.click()
    _flush_deletes()

    assert [s[2] for s in harness.holder._sections] == [kept]
    assert _is_destroyed(box)
    assert _is_destroyed(doomed)
    assert _is_destroyed(button)
    assert not _is_destroyed(kept)


def test_remove_drops_the_name_from_the_picker(app: QApplication) -> None:
    harness = Harness()
    harness.add("[bash]", "bash", [("git *", "allow")])
    harness.add("[read]", "read", [("a", "allow")])

    harness.button(harness.editors[0]).click()
    _flush_deletes()

    box = harness.holder.section_box
    assert [box.itemText(i) for i in range(box.count())] == [
        ScopedHolder.ALL_SECTIONS,
        "[read]",
    ]


def test_removing_every_section_restores_the_hint(app: QApplication) -> None:
    harness = Harness()
    editor = harness.add("[bash]", "bash", [("git *", "allow")])

    harness.button(editor).click()
    _flush_deletes()

    assert harness.holder._sections == []
    assert not harness.holder._empty_hint.isHidden()


def test_read_only_disables_the_remove_button(app: QApplication) -> None:
    harness = Harness()
    editor = harness.add("[bash]", "bash", [("git *", "allow")])

    harness.holder.set_read_only(True)
    assert not harness.button(editor).isEnabled()

    harness.holder.set_read_only(False)
    assert harness.button(editor).isEnabled()


@pytest.mark.parametrize(
    ("rows", "all_effect", "default", "unknown", "expected"),
    [
        ([], None, None, {}, True),
        ([("git *", "allow")], None, None, {}, False),
        ([], "deny", None, {}, False),
        ([], None, "deny", {}, False),
        ([], None, None, {"x": 1}, False),
    ],
)
def test_is_empty_counts_every_kind_of_content(
    app: QApplication,
    rows: list[tuple[str, str]],
    all_effect: str | None,
    default: str | None,
    unknown: dict,
    expected: bool,
) -> None:
    editor = ScopeRulesEditor(lambda: None, "bash")
    editor.set_section(default, all_effect)
    editor.set_unknown(unknown)
    for scope, effect in rows:
        editor.add_row(scope, effect)
    assert editor.is_empty() is expected
