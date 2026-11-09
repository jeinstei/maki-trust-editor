"""The section-default control keeps an invalid stored value visible.

maki rejects any ``default`` that is not allow/deny/prompt (an empty string
becomes an absent key on its side), but dropping the row would silently erase
the value on our save. Instead the box shows an ``(invalid)`` sentinel, so the
row stays and an untouched save preserves the raw text.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "minimal")

from qtpy.QtWidgets import QApplication  # noqa: E402

from maki_trust_editor.widgets import INVALID_LABEL, UNSET_LABEL, ScopeRulesEditor  # noqa: E402


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _editor(default: str | None) -> ScopeRulesEditor:
    editor = ScopeRulesEditor(lambda: None)
    editor.set_section(default, None)
    return editor


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, None),
        ("allow", "allow"),
        ("deny", "deny"),
        ("prompt", "prompt"),
        ("", ""),
        ("bogus", "bogus"),
    ],
)
def test_invalid_default_is_visible_and_preserved(
    app: QApplication, stored: str | None, expected: str | None
) -> None:
    editor = _editor(stored)
    assert editor.default() == expected


def test_unset_shows_unset_label(app: QApplication) -> None:
    assert _editor(None).default_box.currentText() == UNSET_LABEL


def test_invalid_shows_invalid_label(app: QApplication) -> None:
    assert _editor("").default_box.currentText() == INVALID_LABEL


def test_replacing_invalid_with_effect_clears_sentinel(app: QApplication) -> None:
    editor = _editor("")
    editor.default_box.setCurrentText("allow")
    assert editor.default() == "allow"
