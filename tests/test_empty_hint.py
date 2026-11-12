"""The empty-state hint shown while a rules area has no sections.

A blank project or global ``permissions.toml`` used to render an empty grey box
with no affordance, which read as "you cannot add anything". The holder now
shows a hint pointing at the add buttons; it appears only when there is really
nothing to show, and never when nothing is selected at all.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "minimal")

from qtpy.QtWidgets import QApplication  # noqa: E402

from maki_trust_editor import permissions  # noqa: E402
from maki_trust_editor.widgets import ScopedHolder, ScopeRulesEditor  # noqa: E402
from maki_trust_editor.window import PermissionsTab  # noqa: E402

HINT = "nothing here yet"


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _editor() -> ScopeRulesEditor:
    return ScopeRulesEditor(lambda: None, "bash")


def _shown(widget) -> bool:
    return not widget.isHidden()


def test_hint_shows_when_there_are_no_sections(app: QApplication) -> None:
    holder = ScopedHolder("Rules", HINT)
    holder.show()
    assert _shown(holder._empty_hint)


def test_hint_hides_once_a_section_is_added(app: QApplication) -> None:
    holder = ScopedHolder("Rules", HINT)
    holder.show()
    holder.add_editor("[bash]", _editor())
    assert not _shown(holder._empty_hint)


def test_no_hint_when_none_was_given(app: QApplication) -> None:
    holder = ScopedHolder("Rules")
    holder.show()
    assert not _shown(holder._empty_hint)


def test_set_empty_hint_hides_it_for_an_empty_string(app: QApplication) -> None:
    holder = ScopedHolder("Rules", HINT)
    holder.show()
    holder.set_empty_hint("")
    assert not _shown(holder._empty_hint)


def test_set_empty_hint_does_not_show_over_existing_sections(app: QApplication) -> None:
    holder = ScopedHolder("Rules", HINT)
    holder.show()
    holder.clear_editors()
    holder.add_editor("[bash]", _editor())
    holder.set_empty_hint(HINT)
    assert not _shown(holder._empty_hint)


def _tab(tmp_path: Path) -> PermissionsTab:
    tab = PermissionsTab("Select a project first.")
    tab.show()
    return tab


def test_blank_project_shows_the_hint(app: QApplication, tmp_path: Path) -> None:
    tab = _tab(tmp_path)
    tab.load(permissions.PermissionsFile(path=tmp_path / ".maki" / "permissions.toml"))
    assert _shown(tab.scoped._empty_hint)


def test_nothing_selected_shows_no_hint(app: QApplication, tmp_path: Path) -> None:
    tab = _tab(tmp_path)
    tab.scoped.set_empty_hint(tab._rules_hint)
    tab.load(None)
    assert not _shown(tab.scoped._empty_hint)


def test_adding_a_tool_section_replaces_the_hint(app: QApplication, tmp_path: Path) -> None:
    tab = _tab(tmp_path)
    tab.load(permissions.PermissionsFile(path=tmp_path / ".maki" / "permissions.toml"))
    assert tab.add_tool_section("bash") is None
    assert not _shown(tab.scoped._empty_hint)
    assert "bash" in tab._file.tools


def _boxes(holder: ScopedHolder) -> list[str]:
    """Every section box currently in the inner layout, by type name."""
    names = []
    for index in range(holder._inner_layout.count()):
        widget = holder._inner_layout.itemAt(index).widget()
        if widget is not None and widget is not holder._empty_hint:
            names.append(type(widget).__name__)
    return names


def test_alternating_empty_and_populated_leaves_no_orphan_boxes(app: QApplication) -> None:
    # Regression: clear_editors used a fixed layout index, so once a section was
    # added ahead of the hint the wrong widget was removed each cycle. Swapping
    # between an empty and a populated area leaked a box every time until
    # switching sessions broke. Section boxes must be removed by identity.
    holder = ScopedHolder("Rules", HINT)
    holder.show()
    for _ in range(4):
        holder.clear_editors()
        assert _boxes(holder) == []
        holder.add_editor("[bash]", _editor())
        assert _boxes(holder) == ["QFrame"]
    holder.clear_editors()
    assert _boxes(holder) == []


def test_hint_survives_repeated_clears(app: QApplication) -> None:
    holder = ScopedHolder("Rules", HINT)
    holder.show()
    holder.add_editor("[bash]", _editor())
    for _ in range(3):
        holder.clear_editors()
        assert holder._empty_hint.text() == HINT
        assert _shown(holder._empty_hint)
        holder.add_editor("[bash]", _editor())
        assert not _shown(holder._empty_hint)
