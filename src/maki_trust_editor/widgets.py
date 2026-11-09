"""Reusable Qt widgets for the trust editor.

``ScopeRulesEditor`` renders one section (a tool, an MCP server, or a session
tool): an optional section ``default`` effect, an "all scopes" effect, and a row
per scope with mutually-exclusive Allow / Deny controls and a Remove button.
Effects a row cannot represent are never invented; ``prompt`` lives only on the
section default, which is where maki accepts it.
"""

from __future__ import annotations

import re

from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .permissions import EFFECT_ALLOW, EFFECT_DENY, EFFECTS

WILDCARDS = "*?"


def compile_filter(pattern: str) -> re.Pattern[str] | None:
    """A matcher for the rules filter, or ``None`` when the pattern is empty.

    Wildcards are the familiar ``*`` (any run) and ``?`` (one character). With
    no wildcard the pattern is a plain case-insensitive substring search, so
    typing ordinary text behaves as expected. Every other character is escaped,
    so regex metacharacters (``.``, ``+``, ``(``, ``[``, ``\\``) are literal and
    can never blow up or match surprisingly.
    """
    pattern = pattern.strip()
    if not pattern:
        return None
    if not any(c in pattern for c in WILDCARDS):
        return re.compile(re.escape(pattern), re.IGNORECASE)
    regex = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    return re.compile(regex, re.IGNORECASE)


def matches(matcher: re.Pattern[str] | None, text: str) -> bool:
    """Whether ``text`` passes the filter; an empty matcher passes everything."""
    if matcher is None:
        return True
    return bool(matcher.search(text))


# All-scopes control offers only these two; ``prompt`` is a section-default-only
# effect, so it must never appear here.
SCOPE_EFFECTS = (EFFECT_ALLOW, EFFECT_DENY)
ALL_SCOPES_LABELS = ("All scopes: (unset)", "All scopes: allow", "All scopes: deny")
# Section-default control: absent key, an invalid stored value (e.g. ""), then
# the three effects maki accepts.
UNSET_LABEL = "(unset)"
INVALID_LABEL = "(invalid)"
DEFAULT_LABELS = (UNSET_LABEL, INVALID_LABEL, *EFFECTS)


class ScopeRow(QWidget):
    """One scope line: a text field plus Allow / Deny / Remove controls."""

    def __init__(self, scope: str, effect: str, on_change, on_remove) -> None:
        super().__init__()
        self._on_change = on_change
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(8)

        self.scope_edit = QLineEdit(scope)
        self.scope_edit.setPlaceholderText("scope pattern, e.g. git *  or  /path/**")
        self.scope_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.scope_edit.editingFinished.connect(self._changed)

        self.allow_box = QCheckBox("Allow")
        self.deny_box = QCheckBox("Deny")
        # Scopes are only ever allow or deny; anything else (a malformed file)
        # falls back to allow rather than leaving both boxes unchecked.
        self.allow_box.setChecked(effect != EFFECT_DENY)
        self.deny_box.setChecked(effect == EFFECT_DENY)
        self.allow_box.stateChanged.connect(self._allow_toggled)
        self.deny_box.stateChanged.connect(self._deny_toggled)

        self._remove = QPushButton("Remove")
        self._remove.setFixedWidth(80)
        self._remove.clicked.connect(lambda: on_remove(self))

        layout.addWidget(self.scope_edit)
        layout.addWidget(self.allow_box)
        layout.addWidget(self.deny_box)
        layout.addWidget(self._remove)

    def set_read_only(self, read_only: bool) -> None:
        enabled = not read_only
        self.scope_edit.setEnabled(enabled)
        self.allow_box.setEnabled(enabled)
        self.deny_box.setEnabled(enabled)
        self._remove.setEnabled(enabled)

    def _allow_toggled(self, state: int) -> None:
        if state == Qt.CheckState.Checked.value:
            self.deny_box.setChecked(False)
        self._changed()

    def _deny_toggled(self, state: int) -> None:
        if state == Qt.CheckState.Checked.value:
            self.allow_box.setChecked(False)
        self._changed()

    def _changed(self) -> None:
        self._on_change()

    def scope(self) -> str:
        return self.scope_edit.text().strip()

    def effect(self) -> str:
        return EFFECT_DENY if self.deny_box.isChecked() else EFFECT_ALLOW


class ScopeRulesEditor(QWidget):
    """Editor for one section (a native tool, an MCP server, or a session tool).

    A section has an optional ``default`` effect, an "all scopes" effect
    (``unset`` / ``allow`` / ``deny``), and a list of ``(scope, effect)`` rows
    where each effect is ``allow`` or ``deny``. ``default`` is offered the full
    effect set (including ``prompt``) because that is what maki accepts as a
    section default; individual scopes only ever allow or deny.
    """

    def __init__(self, on_change) -> None:
        super().__init__()
        self._on_change = on_change
        self._rows: list[ScopeRow] = []
        self._default_box_visible = True
        # A default maki would reject (e.g. "") is kept verbatim here so the row
        # stays visible and choosing a real effect replaces it instead of losing
        # it. Only consulted while the box sits on the invalid sentinel.
        self._invalid_default: str | None = None
        self._loading = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        header = QHBoxLayout()
        self.default_box = QComboBox()
        self.default_box.addItems(DEFAULT_LABELS)
        self.default_box.currentIndexChanged.connect(lambda _: self._notify())
        self.all_box = QComboBox()
        self.all_box.addItems(ALL_SCOPES_LABELS)
        self.all_box.currentIndexChanged.connect(lambda _: self._notify())

        header.addWidget(QLabel("Section default:"))
        header.addWidget(self.default_box)
        header.addWidget(self.all_box)
        header.addStretch(1)
        outer.addLayout(header)

        self._rows_box = QVBoxLayout()
        self._rows_box.setSpacing(0)
        outer.addLayout(self._rows_box)

        self._add = QPushButton("+ Add scope")
        self._add.clicked.connect(lambda: self.add_row("", EFFECT_ALLOW))
        outer.addWidget(self._add, alignment=Qt.AlignmentFlag.AlignLeft)

        self._unknown_label = QLabel()
        self._unknown_label.setWordWrap(True)
        self._unknown_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._unknown_label.setStyleSheet("font-family: monospace; color: #888;")
        self._unknown_label.hide()
        outer.addWidget(self._unknown_label)

    def set_unknown(self, keys: dict) -> None:
        """Show unmodeled keys preserved in this section, or hide when none."""
        if not keys:
            self._unknown_label.hide()
            return
        rendered = ", ".join(sorted(keys))
        self._unknown_label.setText(f"preserved keys (not editable): {rendered}")
        self._unknown_label.show()

    def set_section(self, default: str | None, all_effect: str | None) -> None:
        """Show the stored default. ``None`` is an absent key; a value maki would
        reject (e.g. ``""``) lands on the ``(invalid)`` sentinel so the row stays
        visible and its raw text survives an untouched save.
        """
        self._loading = True
        try:
            self._invalid_default = None
            if default is None:
                index = 0
            else:
                index = self.default_box.findText(default)
                if index < 0:
                    self._invalid_default = default
                    index = self.default_box.findText(INVALID_LABEL)
            self.default_box.setCurrentIndex(index)
            all_index = (SCOPE_EFFECTS.index(all_effect) + 1) if all_effect in SCOPE_EFFECTS else 0
            self.all_box.setCurrentIndex(all_index)
        finally:
            self._loading = False

    def _notify(self) -> None:
        """Tell the owner the rules changed, unless we are loading them."""
        if not self._loading:
            self._on_change()

    def add_row(self, scope: str, effect: str) -> None:
        row = ScopeRow(scope, effect, self._notify, self._remove_row)
        self._rows.append(row)
        self._rows_box.addWidget(row)
        self._notify()

    def set_read_only(self, read_only: bool) -> None:
        """Disable every control, leaving the rules visible but uneditable."""
        enabled = not read_only
        self.default_box.setEnabled(enabled)
        self.all_box.setEnabled(enabled)
        self._add.setEnabled(enabled)
        for row in self._rows:
            row.set_read_only(read_only)

    def _remove_row(self, row: ScopeRow) -> None:
        self._rows.remove(row)
        row.setParent(None)
        row.deleteLater()
        self._notify()

    def clear(self) -> None:
        for row in list(self._rows):
            self._rows.remove(row)
            row.setParent(None)
            row.deleteLater()

    def default(self) -> str | None:
        text = self.default_box.currentText()
        if text == UNSET_LABEL:
            return None
        if text == INVALID_LABEL:
            return self._invalid_default
        return text

    def all_effect(self) -> str | None:
        if self.all_box.currentIndex() == 0:
            return None
        return SCOPE_EFFECTS[self.all_box.currentIndex() - 1]

    def rows(self) -> list[tuple[str, str]]:
        return [(row.scope(), row.effect()) for row in self._rows if row.scope()]

    def apply_filter(self, matcher: re.Pattern[str] | None) -> int:
        """Hide scope rows that don't match; return how many stay visible.

        Visual only: hidden rows are still collected on save. ``None`` shows
        every row.
        """
        visible = 0
        for row in self._rows:
            match = matches(matcher, row.scope())
            row.setVisible(match)
            visible += match
        return visible


class ScopedHolder(QWidget):
    """A titled, scrollable container of one or more ``ScopeRulesEditor``.

    The heading row carries an optional section picker to the right of the
    title, and a collapsible filter box sits below it. Both narrow what is
    shown; neither touches the model, so hidden sections are still saved.
    """

    ALL_SECTIONS = "All sections"

    def __init__(self, title: str) -> None:
        super().__init__()
        self._sections: list[tuple[str, QFrame, ScopeRulesEditor]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        heading_row = QHBoxLayout()
        heading = QLabel(title)
        heading.setStyleSheet("font-weight: 600;")
        heading_row.addWidget(heading)
        heading_row.addStretch(1)
        self.section_box = QComboBox()
        self.section_box.setToolTip("Show only one rule section")
        self.section_box.addItem(self.ALL_SECTIONS)
        self.section_box.currentIndexChanged.connect(lambda _: self._apply())
        heading_row.addWidget(self.section_box)
        self._toggle = QPushButton("Filter")
        self._toggle.setCheckable(True)
        self._toggle.toggled.connect(self._toggle_filter)
        heading_row.addWidget(self._toggle)
        layout.addLayout(heading_row)

        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("filter rules (* and ? wildcards, Enter to apply)")
        self.filter_box.setClearButtonEnabled(True)
        self.filter_box.returnPressed.connect(self._apply)
        self.filter_box.textChanged.connect(self._filter_text_changed)
        self.filter_box.hide()
        layout.addWidget(self.filter_box)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.StyledPanel)
        self._inner = QWidget()
        self._inner_layout = QVBoxLayout(self._inner)
        self._inner_layout.setContentsMargins(8, 8, 8, 8)
        self._inner_layout.setSpacing(12)
        self._inner_layout.addStretch(1)
        self._scroll.setWidget(self._inner)
        layout.addWidget(self._scroll)

    def add_editor(self, label: str, editor: ScopeRulesEditor) -> None:
        box = QFrame()
        box.setFrameShape(QFrame.Shape.StyledPanel)
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(6, 6, 6, 6)
        name = QLabel(label)
        name.setStyleSheet("font-weight: 600;")
        box_layout.addWidget(name)
        box_layout.addWidget(editor)
        self._inner_layout.insertWidget(self._inner_layout.count() - 1, box)
        self._sections.append((label, box, editor))
        self._rebuild_section_box()

    def clear_editors(self) -> None:
        while self._inner_layout.count() > 1:
            item = self._inner_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._sections.clear()
        self._rebuild_section_box()

    def set_read_only(self, read_only: bool) -> None:
        """Make every section uneditable (used for a session maki holds open)."""
        for _, _, editor in self._sections:
            editor.set_read_only(read_only)

    def refresh(self) -> None:
        """Re-apply the current filter and picker to the loaded sections.

        Newly added section boxes start visible, so without this the holder would
        show sections a still-active filter or picker should hide (and a stale
        hidden state could survive a reload). Also keeps the filter box open
        while it holds text, so the filter that narrowed the list stays visible
        rather than looking collapsed and empty.
        """
        if self.filter_box.text().strip() and not self._toggle.isChecked():
            self._toggle.setChecked(True)
        self._apply()

    def section_names(self) -> list[str]:
        return [label for label, _, _ in self._sections]

    def _rebuild_section_box(self) -> None:
        previous = self.section_box.currentText()
        self.section_box.blockSignals(True)
        self.section_box.clear()
        self.section_box.addItem(self.ALL_SECTIONS)
        self.section_box.addItems(self.section_names())
        index = self.section_box.findText(previous)
        self.section_box.setCurrentIndex(index if index >= 0 else 0)
        self.section_box.blockSignals(False)

    def _toggle_filter(self, shown: bool) -> None:
        self.filter_box.setVisible(shown)
        if not shown:
            self.filter_box.clear()
        else:
            self.filter_box.setFocus()

    def _filter_text_changed(self, text: str) -> None:
        # Applying on every keystroke would recompile the pattern each time and
        # reflow the list mid-typing. Only an emptied box applies immediately,
        # so clearing the field restores the full list; otherwise Enter applies.
        if not text.strip():
            self._apply()

    def _apply(self) -> None:
        matcher = compile_filter(self.filter_box.text())
        wanted = self.section_box.currentText()
        for label, box, editor in self._sections:
            name_match = wanted == self.ALL_SECTIONS or label == wanted
            visible_rows = editor.apply_filter(matcher)
            # A section survives when the picker allows it and either its name
            # matches or one of its scope rows does. With no filter and no
            # picker, a section with no rows still shows.
            show = name_match and (matcher is None or visible_rows or matches(matcher, label))
            box.setVisible(show)
