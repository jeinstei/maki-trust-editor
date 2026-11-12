"""The rules filter and section picker narrow what is shown, never the model.

Hidden sections and rows must still be collected on save, so these drive the
widgets directly and assert on visibility only.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "minimal")

from qtpy.QtWidgets import QApplication  # noqa: E402

from maki_trust_editor.widgets import ScopedHolder, ScopeRulesEditor  # noqa: E402


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _section(label: str, rows: list[tuple[str, str]]) -> ScopeRulesEditor:
    editor = ScopeRulesEditor(lambda: None, "bash")
    for scope, effect in rows:
        editor.add_row(scope, effect)
    return editor


@pytest.fixture
def holder(app: QApplication) -> ScopedHolder:
    holder = ScopedHolder("Rules")
    holder.add_editor("[bash]", _section("bash", [("git *", "deny"), ("ls *", "allow")]))
    holder.add_editor("[edit]", _section("edit", [("*.py", "allow")]))
    holder.add_editor("[empty]", _section("empty", []))
    return holder


def _box_visible(holder: ScopedHolder) -> dict[str, bool]:
    return {label: not box.isHidden() for label, box, _ in holder._sections}


def _row_visible(editor: ScopeRulesEditor) -> list[bool]:
    return [not row.isHidden() for row in editor._rows]


def _filter(holder: ScopedHolder, text: str) -> None:
    """Type a pattern and apply it, as pressing Enter in the box would."""
    holder.filter_box.setText(text)
    holder._apply()


def test_section_picker_lists_all_and_each_name(holder: ScopedHolder) -> None:
    items = [holder.section_box.itemText(i) for i in range(holder.section_box.count())]
    assert items == [ScopedHolder.ALL_SECTIONS, "[bash]", "[edit]", "[empty]"]


def test_filter_hides_matching_rows_and_empty_sections(holder: ScopedHolder) -> None:
    _filter(holder, "git")

    assert _box_visible(holder) == {"[bash]": True, "[edit]": False, "[empty]": False}
    assert _row_visible(holder._sections[0][2]) == [True, False]


def test_filter_matches_section_name(holder: ScopedHolder) -> None:
    _filter(holder, "edit")

    assert _box_visible(holder) == {"[bash]": False, "[edit]": True, "[empty]": False}


def test_filter_is_not_applied_until_enter(holder: ScopedHolder) -> None:
    holder.filter_box.setText("git")

    assert _box_visible(holder) == {"[bash]": True, "[edit]": True, "[empty]": True}


@pytest.mark.parametrize(
    ("pattern", "section", "visible"),
    [
        ("git*", 0, [True, False]),
        ("ls ?", 0, [False, True]),
        ("py", 1, [True]),
        ("*.py", 1, [True]),
        ("[", 0, [False, False]),
        ("?*.py", 1, [True]),
    ],
    ids=["star", "question", "substring", "wildcard-dot", "bad-pattern", "question-one-char"],
)
def test_filter_pattern_selects_rows(holder: ScopedHolder, pattern, section, visible) -> None:
    """Wildcards, plain substrings and literal regex characters all narrow as documented.

    A pattern with no wildcard is a substring match, ``*`` runs and ``?`` takes
    exactly one character, and a regex metacharacter is literal: an unclosed
    ``[`` matches nothing rather than raising.
    """
    _filter(holder, pattern)

    assert _row_visible(holder._sections[section][2]) == visible


def test_question_wildcard_takes_exactly_one_character(holder: ScopedHolder) -> None:
    """``?`` is one character, not a run of them.

    Checked on the matcher directly: through a row the difference between ``?``
    and ``.*`` lands on the same visible result, so a row assertion would not
    pin it.
    """
    from maki_trust_editor.widgets import compile_filter, matches

    matcher = compile_filter("a?c")
    assert matches(matcher, "abc")
    assert not matches(matcher, "abbc")


def test_picker_shows_only_that_section(holder: ScopedHolder) -> None:
    holder.section_box.setCurrentText("[edit]")

    assert _box_visible(holder) == {"[bash]": False, "[edit]": True, "[empty]": False}


def test_picker_and_filter_combine_with_and(holder: ScopedHolder) -> None:
    holder.section_box.setCurrentText("[bash]")
    _filter(holder, "git")

    assert _box_visible(holder) == {"[bash]": True, "[edit]": False, "[empty]": False}
    assert _row_visible(holder._sections[0][2]) == [True, False]


def test_clearing_filter_restores_everything(holder: ScopedHolder) -> None:
    _filter(holder, "git")
    holder.filter_box.clear()

    assert _box_visible(holder) == {"[bash]": True, "[edit]": True, "[empty]": True}
    for label, _, editor in holder._sections:
        assert _row_visible(editor) == [True] * len(editor._rows), label


def test_hidden_rows_are_still_collected(holder: ScopedHolder) -> None:
    _filter(holder, "git")

    assert holder._sections[0][2].rows() == [("git *", "deny"), ("ls *", "allow")]


def test_toggle_button_shows_and_clears_filter(holder: ScopedHolder) -> None:
    assert holder.filter_box.isHidden()

    holder.filter_box.setText("git")
    holder._toggle.setChecked(True)
    assert not holder.filter_box.isHidden()

    holder._toggle.setChecked(False)
    assert holder.filter_box.isHidden()
    assert holder.filter_box.text() == ""


def test_bad_pattern_does_not_raise(holder: ScopedHolder) -> None:
    _filter(holder, "([")  # regex metacharacters, treated literally

    assert _box_visible(holder) == {"[bash]": False, "[edit]": False, "[empty]": False}


def _reload(holder: ScopedHolder, sections: list[tuple[str, list[tuple[str, str]]]]) -> None:
    """Swap in a new set of sections and refresh, as a session switch does."""
    holder.clear_editors()
    for label, rows in sections:
        holder.add_editor(label, _section(label, rows))
    holder.refresh()


def test_refresh_reapplies_active_filter_after_reload(holder: ScopedHolder) -> None:
    _filter(holder, "git")
    _reload(holder, [("[bash]", [("git *", "deny")]), ("[edit]", [("*.py", "allow")])])

    # The filter 'git' is still active, so only [bash] survives the reload.
    assert _box_visible(holder) == {"[bash]": True, "[edit]": False}


def test_refresh_keeps_filter_box_open_when_text_remains(holder: ScopedHolder) -> None:
    holder._toggle.setChecked(True)
    holder.filter_box.setText("git")
    _reload(holder, [("[bash]", [("git *", "deny")])])

    assert not holder.filter_box.isHidden()
    assert holder._toggle.isChecked()


def test_reload_resets_picker_to_all_sections(holder: ScopedHolder) -> None:
    # The picker is per-view state and does not survive a reload; the filter does.
    holder.section_box.setCurrentText("[edit]")
    _reload(holder, [("[bash]", [("git *", "deny")]), ("[edit]", [("*.py", "allow")])])

    assert holder.section_box.currentText() == ScopedHolder.ALL_SECTIONS
    assert _box_visible(holder) == {"[bash]": True, "[edit]": True}
