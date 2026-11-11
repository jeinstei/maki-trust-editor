"""The main window: four tabs, each editing one store, one Save button.

Layout:

    * Sessions  - pick a session, edit its rules as allow/deny scope rows.
    * Projects  - pick a discovered project, edit its .maki/permissions.toml.
    * Folders   - the folder-trust document plus the frozen pre-trust snapshot.
    * Global    - the user-wide ~/.config/maki/permissions.toml.

Save writes only the stores whose content changed since load, so an untouched
file is never rewritten. Every write goes through ``safe_write``: it backs the
file up first and refuses while a maki process is running.
"""

from __future__ import annotations

import datetime
import logging
import re
import time

from qtpy.QtCore import Qt, QTimer
from qtpy.QtGui import QFontMetrics, QKeySequence, QPalette, QShortcut
from qtpy.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import changes, folders, paths, permissions, safe_write, sessions, workspace
from .permissions import EFFECT_ALLOW, McpServerRules, ToolRules
from .widgets import ScopedHolder, ScopeRulesEditor

MAKI_STATUS_TTL = 2.0
# How often the live-refresh tick re-probes maki and the visible sessions. Fast
# enough that a lock acquired mid-edit shows within a couple of seconds, slow
# enough that the process-table scan is negligible while idle.
LIVENESS_POLL_MS = 2000
TOOL_NAME_RE = re.compile(r"^\w[\w-]*$")
DIRTY_MARK = " *"
# The id a session was named by embeds a UUIDv7 timestamp in milliseconds while
# the header records ``created_at`` in whole seconds, so the two differ by under
# a second even when they agree. Anything past this is a real disagreement.
CREATED_AT_TOLERANCE_S = 2
# Tall enough to list a full session's rules without scrolling to find the point.
SAVE_DIALOG_LIST_HEIGHT = 260
# Tab labels in addTab order, so titles can be rebuilt with the dirty marker
# without repeating the strings.
TAB_TITLES = ("Sessions", "Projects", "Folders", "Global")
SESSIONS_TAB = TAB_TITLES.index("Sessions")
PROJECTS_TAB = TAB_TITLES.index("Projects")
FOLDERS_TAB = TAB_TITLES.index("Folders")
GLOBAL_TAB = TAB_TITLES.index("Global")

# Marks a session another maki holds open. The padlock is the clear glyph; the
# filled dot is a fallback for a font that lacks it, chosen once via
# ``QFontMetrics.inFont`` so the list never shows a tofu box.
LOCK_ICON = "\N{LOCK}"
LOCK_ICON_FALLBACK = "\N{BLACK CIRCLE}"
_LOCK_ICON_CACHE: str | None = None

LOGGER = logging.getLogger("maki_trust_editor.window")


def _rules_all_effect(rules: ToolRules | McpServerRules) -> str | None:
    if rules.allow_all:
        return EFFECT_ALLOW
    if rules.deny_all:
        return "deny"
    return None


def _assign_all_effect(rules: ToolRules | McpServerRules, effect: str | None) -> None:
    rules.allow_all = effect == EFFECT_ALLOW
    rules.deny_all = effect == "deny"


def _lock_prefix(locked: bool) -> str:
    """A session-open marker with a trailing space, or an empty string.

    Resolves the padlock against the application font once: a font without the
    glyph falls back to the filled dot rather than rendering a tofu box.
    """
    global _LOCK_ICON_CACHE
    if not locked:
        return ""
    if _LOCK_ICON_CACHE is None:
        metrics = QFontMetrics(QApplication.font())
        has_glyph = metrics.inFontUcs4(ord(LOCK_ICON))
        _LOCK_ICON_CACHE = LOCK_ICON if has_glyph else LOCK_ICON_FALLBACK
    return _LOCK_ICON_CACHE + " "


class PermissionsTab(QWidget):
    """Editable view of a single ``permissions.toml`` (global or one project)."""

    def __init__(self, empty_hint: str, on_change=None) -> None:
        super().__init__()
        self._empty_hint = empty_hint
        self._on_change = on_change or (lambda: None)
        self._file: permissions.PermissionsFile | None = None
        self._rules_hint = self.tr(
            "No rules yet — use the + buttons below to add a tool or MCP server."
        )

        layout = QVBoxLayout(self)
        self.path_label = QLabel(empty_hint)
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path_label.setStyleSheet("font-family: monospace;")
        self.path_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        layout.addWidget(self.path_label)

        self.scoped = ScopedHolder("Rules", self._rules_hint)
        layout.addWidget(self.scoped)
        self._editors: list[tuple[str, ScopeRulesEditor, bool]] = []

    def load(self, file: permissions.PermissionsFile | None) -> None:
        self._file = file
        self.scoped.clear_editors()
        self._editors.clear()
        if file is None:
            self.path_label.setText(self._empty_hint)
            self.scoped.set_empty_hint("")
            return
        self.scoped.set_empty_hint(self._rules_hint)
        state = self.tr("exists") if file.exists else self.tr("will be created on save")
        self.path_label.setText(
            self.tr("file: {path}\n({state})").format(path=file.path, state=state)
        )
        for name in sorted(file.tools):
            self._add_tool(name, file.tools[name])
        for server in sorted(file.mcp):
            self._add_mcp(server, file.mcp[server])
        self.scoped.refresh()

    def _add_tool(self, name: str, rules: ToolRules) -> None:
        editor = ScopeRulesEditor(self._on_change)
        editor.set_section(rules.default, _rules_all_effect(rules))
        editor.set_unknown(rules.unknown)
        for scope in rules.deny:
            editor.add_row(scope, "deny")
        for scope in rules.allow:
            editor.add_row(scope, "allow")
        self.scoped.add_editor(f"[{name}]", editor)
        self._editors.append((name, editor, False))

    def _add_mcp(self, server: str, rules: McpServerRules) -> None:
        editor = ScopeRulesEditor(self._on_change)
        editor.set_section(rules.default, _rules_all_effect(rules))
        editor.set_unknown(rules.unknown)
        for scope in rules.deny:
            editor.add_row(scope, "deny")
        for scope in rules.allow:
            editor.add_row(scope, "allow")
        self.scoped.add_editor(f"[mcp.{server}]", editor)
        self._editors.append((server, editor, True))

    def add_tool_section(self, name: str) -> str | None:
        """Add a ``[name]`` section, or return an error message."""
        return self._add_section(name, is_mcp=False)

    def add_mcp_section(self, server: str) -> str | None:
        """Add an ``[mcp.server]`` section, or return an error message."""
        return self._add_section(server, is_mcp=True)

    def _add_section(self, name: str, is_mcp: bool) -> str | None:
        if self._file is None:
            return self.tr("Select a project first.")
        if not TOOL_NAME_RE.match(name):
            return self.tr(
                "Names must start with a letter, digit or underscore and contain "
                "only letters, digits, underscores and hyphens."
            )
        if name in self._file.tools:
            return self.tr("A tool section named '{name}' already exists.").format(name=name)
        if name in self._file.mcp:
            return self.tr("An MCP section named '{name}' already exists.").format(name=name)
        if is_mcp:
            self._file.mcp[name] = McpServerRules(server=name)
            self._add_mcp(name, self._file.mcp[name])
        else:
            self._file.tools[name] = ToolRules(name=name)
            self._add_tool(name, self._file.tools[name])
        return None

    def collect(self) -> permissions.PermissionsFile | None:
        """Write the widget state back into the model and return it."""
        if self._file is None:
            return None
        previous = self._file
        self._file.tools.clear()
        self._file.mcp.clear()
        for name, editor, is_mcp in self._editors:
            if is_mcp:
                old = previous.mcp.get(name)
                mcp_rules = McpServerRules(
                    server=name,
                    default=editor.default(),
                    unknown=dict(old.unknown) if old else {},
                )
                _assign_all_effect(mcp_rules, editor.all_effect())
                for scope, effect in editor.rows():
                    (mcp_rules.deny if effect == "deny" else mcp_rules.allow).append(scope)
                self._file.mcp[name] = mcp_rules
            else:
                old = previous.tools.get(name)
                tool_rules = ToolRules(
                    name=name,
                    default=editor.default(),
                    unknown=dict(old.unknown) if old else {},
                )
                _assign_all_effect(tool_rules, editor.all_effect())
                for scope, effect in editor.rows():
                    (tool_rules.deny if effect == "deny" else tool_rules.allow).append(scope)
                self._file.tools[name] = tool_rules
        return self._file


class SessionsTab(QWidget):
    def __init__(self, on_change, is_dirty, rebaselined) -> None:
        super().__init__()
        self._views: list[workspace.SessionView] = []
        self._current: workspace.Session | None = None
        self._on_change = on_change
        self._is_dirty = is_dirty
        self._rebaselined = rebaselined

        layout = QHBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._shown = -1
        self.loading = False

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel(self.tr("Sessions")))
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._selected)
        left_layout.addWidget(self.list)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.file_label = QLabel(self.tr("file: (none)"))
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.file_label.setStyleSheet("font-family: monospace;")
        self.file_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.file_label.setWordWrap(True)
        right_layout.addWidget(self.file_label)
        self.header = QLabel(self.tr("Select a session"))
        self.header.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.header.setWordWrap(True)
        right_layout.addWidget(self.header)
        self._rules_hint = self.tr("No rules yet — use '+ Add tool group' below to start.")
        self.scoped = ScopedHolder(
            self.tr("Session rules (allow/deny per scope)"), self._rules_hint
        )
        right_layout.addWidget(self.scoped)
        self._editors: list[tuple[str, ScopeRulesEditor]] = []

        self._add_button = QPushButton(self.tr("+ Add tool group"))
        self._add_button.clicked.connect(self._add_group)
        self._add_button.setEnabled(False)
        right_layout.addWidget(self._add_button, alignment=Qt.AlignmentFlag.AlignLeft)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 2)

        layout.addWidget(splitter)

    def load(self, views: list[workspace.SessionView]) -> None:
        self._views = views
        self._labels = []
        self.list.blockSignals(True)
        self.list.clear()
        for view in views:
            self._labels.append(self.label_for(view))
            item = QListWidgetItem(self._labels[-1])
            item.setToolTip(self._tooltip_for(view))
            self.list.addItem(item)
        self.list.blockSignals(False)
        self._selected(self.list.currentRow())

    def label_for(self, view: workspace.SessionView) -> str:
        return f"{_lock_prefix(view.locked)}{view.label}\n{view.subtitle}"

    def _tooltip_for(self, view: workspace.SessionView) -> str:
        return (self.tr("open in maki\n") if view.locked else "") + view.subtitle

    def sync_labels(self) -> None:
        """Rewrite the row text and tooltip from the current views.

        The lock glyph and tooltip are derived from each view, so a lock the
        poll acquired or released is only visible after this runs.
        """
        self._labels = [self.label_for(view) for view in self._views]
        for row, view in enumerate(self._views):
            item = self.list.item(row)
            item.setText(self._labels[row])
            item.setToolTip(self._tooltip_for(view))

    def row_label(self, row: int) -> str:
        return self._labels[row]

    def _selected(self, row: int) -> None:
        if row != self._shown:
            # Park the outgoing session's edits in its model so they survive the
            # switch (dirty persists across clicks) instead of being dropped.
            self._on_change()
        self._populate_row(row)

    def _populate_row(self, row: int, *, reload: bool = True) -> None:
        self._shown = row
        self.loading = True
        self.scoped.clear_editors()
        self._editors.clear()
        if row < 0 or row >= len(self._views):
            self._current = None
            self._add_button.setEnabled(False)
            self.file_label.setText(self.tr("file: (none)"))
            self.header.setText(self.tr("Select a session"))
            self.scoped.set_empty_hint("")
            self.loading = False
            return
        view = self._views[row]
        # Re-read before showing: a live session grows while maki runs, so the
        # copy loaded at startup has a stale last-line offset. Loading fresh
        # keeps the rules (and the offset a save writes from) current. A row
        # with a pending edit is left alone: reloading would drop it.
        #
        # A clean reload moves the baseline with it: the fresh copy is the truth,
        # not an edit, and comparing it against the baseline taken at startup
        # would flag a session that changed on disk (maki rewriting it) as dirty
        # the moment it is opened. The lock path re-baselines for the same reason.
        if reload and not self._is_dirty(row):
            if view.reload():
                self._rebaselined(view)
        session = view.session
        self._current = session
        self.scoped.set_empty_hint(self._rules_hint)
        self.file_label.setText(self.tr("file: {path}").format(path=session.path))
        self.header.setText(self._session_header(session))
        for group in sessions.ToolGroup.group(session.rules):
            editor = ScopeRulesEditor(self._on_change)
            editor.set_section(None, group.all_effect)
            for scope, effect in group.scopes:
                editor.add_row(scope, effect)
            self.scoped.add_editor(group.tool, editor)
            self._editors.append((group.tool, editor))
        # A session maki holds open is shown but not editable: any write would
        # race with maki re-appending to the same file on exit.
        self.set_read_only(view.locked)
        self.scoped.refresh()
        self.loading = False

    def set_read_only(self, read_only: bool) -> None:
        """Enable or disable editing of the shown session in place."""
        self.scoped.set_read_only(read_only)
        self._add_button.setEnabled(not read_only)

    def _id_line(self, session_id: str) -> str:
        """The session id, shown with the UUID it decodes to.

        A base58 id (what maki writes now) is opaque, so the decoded UUID and
        version are appended: ``<raw> - <uuid> (vN)``. When the raw form already
        *is* the UUID (a legacy hex id) the two would read as a duplicate, so
        only the single ``<uuid> (vN)`` is shown. An id that does not parse is
        shown raw and nothing more.
        """
        decoded = sessions.decode_session_id(session_id)
        if decoded is None:
            return session_id
        labelled = f"{decoded.uuid} (v{decoded.version})"
        if decoded.uuid == session_id.lower():
            return labelled
        return f"{session_id} - {labelled}"

    def _created_line(self, session_id: str, created_at: int) -> str:
        """The creation time decoded from the id, and whether it matches the header.

        The id encodes the time it was generated; the header records its own
        ``created_at``. They should agree, so a disagreement is called out: it
        means the id and the header did not come from the same creation. A
        legacy id carries no time of its own, which is reported as ``(none)``
        rather than mismatched.
        """
        decoded = sessions.decode_session_id(session_id)
        if decoded is None or decoded.created is None:
            return self.tr("created: (none)")
        stamp = decoded.created.strftime("%Y-%m-%d %H:%M:%S UTC")
        text = self.tr("created: {stamp}").format(stamp=stamp)
        if created_at:
            delta = abs(decoded.created.timestamp() - created_at)
            if delta > CREATED_AT_TOLERANCE_S:
                text += self.tr("  (differs from header: {header})").format(
                    header=self._utc_stamp(created_at)
                )
        return text

    @staticmethod
    def _utc_stamp(seconds: int) -> str:
        moment = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
        return moment.strftime("%Y-%m-%d %H:%M:%S UTC")

    def _session_header(self, session: sessions.Session) -> str:
        return self.tr("{id}\n{created}\ncwd: {cwd}\nmodel: {model}\n{count} rule(s)").format(
            id=self._id_line(session.session_id),
            created=self._created_line(session.session_id, session.created_at),
            cwd=session.cwd,
            model=session.model,
            count=len(session.rules),
        )

    def commit(self) -> None:
        """Park the shown session's editor content in its model.

        Called before switching away, so an edit survives the click and still
        counts as dirty later. The model keeps the rules; the file is untouched
        until Save.
        """
        if self._current is not None:
            self._current.rules = self.collect()

    def _add_group(self) -> None:
        if self._current is None:
            return
        name, ok = QInputDialog.getText(
            self, self.tr("Add tool group"), self.tr("Tool name (e.g. bash):")
        )
        name = name.strip()
        if not ok or not name:
            return
        if not TOOL_NAME_RE.match(name):
            QMessageBox.warning(
                self,
                self.tr("Cannot add group"),
                self.tr(
                    "Names must start with a letter, digit or underscore and contain "
                    "only letters, digits, underscores and hyphens."
                ),
            )
            return
        if any(tool == name for tool, _ in self._editors):
            QMessageBox.warning(
                self,
                self.tr("Cannot add group"),
                self.tr("A group named '{name}' already exists.").format(name=name),
            )
            return
        editor = ScopeRulesEditor(self._on_change)
        editor.add_row("", EFFECT_ALLOW)
        self.scoped.add_editor(name, editor)
        self._editors.append((name, editor))

    def collect(self) -> list:
        rules: list[sessions.SessionRule] = []
        for tool, editor in self._editors:
            group = sessions.ToolGroup(
                tool=tool,
                all_effect=editor.all_effect(),
                scopes=editor.rows(),
            )
            rules.extend(group.to_rules())
        return rules


class SaveDialog(QDialog):
    """Show what a save will write, and let the user back out.

    A rule can be edited in a section that is not on screen, so the confirmation
    lists the changes rather than trusting the visible editor to have shown
    them. Cancel writes nothing and leaves every edit pending.
    """

    def __init__(self, parent, review: changes.ChangeList):
        super().__init__(parent)
        self.review = review
        self.setWindowTitle(self.tr("Save changes"))
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                self.tr("{path}\n\nThese changes will be written to disk:").format(path=review.path)
            )
        )
        listed = QListWidget()
        for change in review.changes:
            listed.addItem(QListWidgetItem(change.line()))
        listed.setMinimumHeight(SAVE_DIALOG_LIST_HEIGHT)
        layout.addWidget(listed)
        if review.stale:
            layout.addWidget(
                QLabel(
                    self.tr(
                        "This file changed on disk since it was loaded; the list "
                        "compares against its current content."
                    )
                )
            )
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("Write"))
        button_box.button(QDialogButtonBox.StandardButton.Cancel).setText(self.tr("Cancel"))
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)


class FolderDialog(QDialog):
    def __init__(self, parent, kind="trusted", path="", files=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Edit Folder Trust"))
        layout = QFormLayout(self)
        self.path_input = QLineEdit(path)
        layout.addRow(self.tr("Path:"), self.path_input)
        self.files_input = QLineEdit(", ".join(files) if files else "")
        layout.addRow(self.tr("Gated files (comma-separated):"), self.files_input)
        self.trusted_radio = QRadioButton(self.tr("Trusted"))
        self.rejected_radio = QRadioButton(self.tr("Rejected"))
        if kind == "trusted":
            self.trusted_radio.setChecked(True)
        else:
            self.rejected_radio.setChecked(True)
        layout.addRow(self.tr("Type:"), self.trusted_radio)
        layout.addRow("", self.rejected_radio)
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addRow(button_box)

    def result(self):
        kind = "trusted" if self.trusted_radio.isChecked() else "rejected"
        path = self.path_input.text().strip()
        files_text = self.files_input.text().strip()
        files = [f.strip() for f in files_text.split(",") if f.strip()] if files_text else []
        return kind, path, files


class FoldersTab(QWidget):
    def __init__(self, on_change=None) -> None:
        super().__init__()
        self._doc = None
        self._pre_trust = None
        self._on_change = on_change or (lambda: None)
        layout = QVBoxLayout(self)
        self.file_label = QLabel(self.tr("file: (none)"))
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.file_label.setStyleSheet("font-family: monospace;")
        self.file_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.file_label.setWordWrap(True)
        layout.addWidget(self.file_label)
        layout.addWidget(
            QLabel(
                self.tr(
                    "Folder trust. Add, edit or remove a trusted folder or a rejected "
                    "path; changes are written on Save."
                )
            )
        )
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._edit_item)
        layout.addWidget(self.list)
        buttons = QHBoxLayout()
        add_btn = QPushButton(self.tr("+ Add folder"))
        add_btn.clicked.connect(self._show_add_dialog)
        buttons.addWidget(add_btn)
        remove = QPushButton(self.tr("Remove selected"))
        remove.clicked.connect(self._remove)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        layout.addWidget(self._build_pre_trust_group())

    def _build_pre_trust_group(self) -> QGroupBox:
        """The frozen pre-trust snapshot: roots that predate folder trust.

        maki reads it once at startup to grandfather roots, so editing it here
        is the only way to fix a snapshot that was frozen wrongly.
        """
        group = QGroupBox(self.tr("Pre-trust roots (frozen snapshot)"))
        box = QVBoxLayout(group)
        self.pre_trust_label = QLabel(self.tr("file: (none)"))
        self.pre_trust_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.pre_trust_label.setStyleSheet("font-family: monospace;")
        self.pre_trust_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.pre_trust_label.setWordWrap(True)
        box.addWidget(self.pre_trust_label)
        box.addWidget(
            QLabel(
                self.tr(
                    "Project roots that predate folder trust and are grandfathered at "
                    "startup. Add or remove paths; changes are written on Save."
                )
            )
        )
        self.pre_trust_list = QListWidget()
        box.addWidget(self.pre_trust_list)
        row = QHBoxLayout()
        add = QPushButton(self.tr("+ Add root"))
        add.clicked.connect(self._add_pre_trust_root)
        row.addWidget(add)
        remove = QPushButton(self.tr("Remove selected"))
        remove.clicked.connect(self._remove_pre_trust_root)
        row.addWidget(remove)
        row.addStretch(1)
        box.addLayout(row)
        return group

    def load(self, doc, pre_trust=None) -> None:
        self._doc = doc
        if doc is not None:
            state = self.tr("exists") if doc.exists else self.tr("will be created on save")
            self.file_label.setText(
                self.tr("file: {path}\n({state})").format(path=doc.path, state=state)
            )
        else:
            self.file_label.setText(self.tr("file: (none)"))
        self._pre_trust = pre_trust
        if pre_trust is not None:
            state = self.tr("exists") if pre_trust.exists else self.tr("will be created on save")
            self.pre_trust_label.setText(
                self.tr("file: {path}\n({state})").format(path=pre_trust.path, state=state)
            )
        else:
            self.pre_trust_label.setText(self.tr("file: (none)"))
        self._refresh()

    def _refresh(self) -> None:
        self.list.clear()
        self.pre_trust_list.clear()
        if self._doc is not None:
            for entry in self._doc.folders:
                files = ", ".join(entry.files) or self.tr("none")
                item = QListWidgetItem(
                    self.tr("[trusted] {path}\n  gated files: {files}").format(
                        path=entry.path, files=files
                    )
                )
                item.setData(Qt.ItemDataRole.UserRole, ("trusted", entry.path))
                self.list.addItem(item)
            for path in self._doc.rejected:
                item = QListWidgetItem(self.tr("[rejected] {path}").format(path=path))
                item.setData(Qt.ItemDataRole.UserRole, ("rejected", path))
                self.list.addItem(item)
        if self._pre_trust is not None:
            for root in self._pre_trust.roots:
                self.pre_trust_list.addItem(root)

    def _add_pre_trust_root(self) -> None:
        if self._pre_trust is None:
            return
        directory = QFileDialog.getExistingDirectory(self, self.tr("Add pre-trust root"))
        if not directory:
            return
        if not self._pre_trust.add(directory):
            QMessageBox.information(
                self,
                self.tr("Already present"),
                self.tr("{path} is already listed.").format(path=directory),
            )
            return
        self._refresh()
        self._on_change()

    def _remove_pre_trust_root(self) -> None:
        item = self.pre_trust_list.currentItem()
        if item is None or self._pre_trust is None:
            return
        self._pre_trust.remove(item.text())
        self._refresh()
        self._on_change()

    def _remove(self) -> None:
        item = self.list.currentItem()
        if item is None or self._doc is None:
            return
        kind, path = item.data(Qt.ItemDataRole.UserRole)
        if kind == "trusted":
            self._doc.remove_folder(path)
        else:
            self._doc.remove_rejected(path)
        self._refresh()
        self._on_change()

    def _show_add_dialog(self) -> None:
        if self._doc is None:
            return
        dialog = FolderDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            kind, path, files = dialog.result()
            if kind == "trusted":
                self._doc.add_or_update_folder(path, files)
            else:
                self._doc.add_rejected(path)
            self._refresh()
            self._on_change()

    def _edit_item(self, item) -> None:
        if self._doc is None:
            return
        kind, path = item.data(Qt.ItemDataRole.UserRole)
        if kind == "trusted":
            entry = next((e for e in self._doc.folders if e.path == path), None)
            if entry:
                dialog = FolderDialog(self, kind="trusted", path=path, files=entry.files)
            else:
                return
        else:
            dialog = FolderDialog(self, kind="rejected", path=path, files=[])
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_kind, new_path, new_files = dialog.result()
            if kind == "trusted" and new_kind == "trusted":
                self._doc.add_or_update_folder(new_path, new_files)
            elif kind == "rejected" and new_kind == "rejected":
                if path != new_path:
                    self._doc.remove_rejected(path)
                self._doc.add_rejected(new_path)
            elif kind == "trusted" and new_kind == "rejected":
                self._doc.remove_folder(path)
                self._doc.add_rejected(new_path)
            elif kind == "rejected" and new_kind == "trusted":
                self._doc.remove_rejected(path)
                self._doc.add_or_update_folder(new_path, new_files)
            self._refresh()
            self._on_change()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("maki trust editor")
        self.resize(1100, 760)

        # The project row whose file the editor currently shows. Distinct from
        # projects_selector.currentRow(), which is already the *incoming* row by
        # the time currentRowChanged fires, so committing to currentRow() would
        # write the visible editor into the wrong project.
        self._shown_project_row = -1
        self._active_tab = SESSIONS_TAB
        self._loading = False
        self._baseline: dict = {}
        self._maki_cache = None
        self._maki_checked_at = 0.0
        self._maki_cache_prev: bool | None = None
        self._force_close = False
        self.workspace = workspace.load_workspace()
        self.add_tool_btn = None
        self.add_mcp_btn = None

        central = QWidget()
        layout = QVBoxLayout(central)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.tabs = QTabWidget()
        self.sessions_tab = SessionsTab(
            self._mark_dirty,
            self._session_row_dirty,
            lambda view: self._rebaseline_session(view, sync=False),
        )
        self.global_tab = PermissionsTab(
            self.tr("No global permissions file yet."), self._mark_dirty
        )
        self.projects_tab = PermissionsTab(self.tr("Select a project."), self._mark_dirty)
        self.folders_tab = FoldersTab(self._mark_dirty)

        # The projects tab pairs a selector with the permissions editor.
        self.projects_selector = QListWidget()
        self.projects_selector.setMaximumWidth(320)
        self.projects_selector.currentRowChanged.connect(self._project_selected)
        projects_container = QWidget()
        pc_layout = QHBoxLayout(projects_container)
        pc_layout.addWidget(self.projects_selector)
        pc_layout.addWidget(self.projects_tab, 1)

        self.tabs.addTab(self.sessions_tab, self._tab_title(SESSIONS_TAB))
        self.tabs.addTab(projects_container, self._tab_title(PROJECTS_TAB))
        self.tabs.addTab(self.folders_tab, self._tab_title(FOLDERS_TAB))
        self.tabs.addTab(self.global_tab, self._tab_title(GLOBAL_TAB))
        self.tabs.currentChanged.connect(self._tab_changed)
        layout.addWidget(self.tabs)

        bar = QHBoxLayout()
        save = QPushButton(self.tr("Save"))
        save.clicked.connect(self._save)
        reload_all = QPushButton(self.tr("Reload all"))
        reload_all.clicked.connect(self._reload)
        self.reload_file_btn = QPushButton(self.tr("Reload file"))
        self.reload_file_btn.clicked.connect(self._reload_current)
        self.add_tool_btn = QPushButton(self.tr("+ Tool section (project)"))
        self.add_tool_btn.clicked.connect(self._add_project_tool)
        self.add_mcp_btn = QPushButton(self.tr("+ MCP server (project)"))
        self.add_mcp_btn.clicked.connect(self._add_project_mcp)
        bar.addWidget(reload_all)
        bar.addWidget(self.reload_file_btn)
        bar.addWidget(self.add_tool_btn)
        bar.addWidget(self.add_mcp_btn)
        bar.addStretch(1)
        bar.addWidget(save)
        layout.addLayout(bar)

        self.setCentralWidget(central)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_theme_menu)
        self._setup_shortcuts()
        self._populate()

        # maki opening or closing a session is invisible to this process unless
        # we look. The tick re-probes the visible rows and the process state; a
        # focus change refreshes at once, so returning to the window is current
        # rather than up to one interval stale.
        self._liveness_timer = QTimer(self)
        self._liveness_timer.setInterval(LIVENESS_POLL_MS)
        self._liveness_timer.timeout.connect(self._refresh_liveness)
        self._liveness_timer.start()

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._save)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self._reload)

    def _show_theme_menu(self, pos) -> None:
        """Right-click menu: pick dark, light, or follow the system.

        The three are mutually exclusive and the saved choice is checked. ``auto``
        carries the theme it currently resolves to, so a system whose scheme maki
        cannot detect (it reports light) is visible rather than a silent surprise.
        Applying happens after the menu closes, since re-styling the app while its
        own popup is open is the one unreliable moment for a live theme switch.
        The theme helpers live in ``app`` and are imported here rather than at
        module scope: ``app`` imports this module, so a top-level import would be
        a cycle.
        """
        import qdarktheme
        from qtpy.QtWidgets import QActionGroup, QMenu

        from .app import apply_theme, load_theme_mode, resolved_theme

        # `auto` is the one label that is not static: it carries the theme the
        # system currently resolves to, so a detection that reports light is
        # visible rather than a silent surprise.
        labels = {
            "dark": self.tr("Dark theme"),
            "light": self.tr("Light theme"),
            "auto": self.tr("Follow system ({theme})").format(theme=resolved_theme("auto")),
        }

        current = load_theme_mode()
        menu = QMenu(self)
        group = QActionGroup(menu)
        group.setExclusive(True)
        actions = {}
        for mode in qdarktheme.get_themes():
            action = menu.addAction(labels[mode])
            action.setCheckable(True)
            action.setChecked(mode == current)
            group.addAction(action)
            actions[action] = mode

        picked = menu.exec(self.mapToGlobal(pos))
        if picked in actions:
            apply_theme(actions[picked])

    def changeEvent(self, event) -> None:
        if event.type() == event.Type.ActivationChange and self.isActiveWindow():
            self._refresh_liveness()
        super().changeEvent(event)

    def _tab_title(self, index: int) -> str:
        """The display title for a tab, as a ``tr`` literal so it extracts.

        ``TAB_TITLES`` stays the stable English tuple used for indexing; this
        maps an index back to a translatable literal for the actual tab text.
        """
        if index == SESSIONS_TAB:
            return self.tr("Sessions")
        if index == PROJECTS_TAB:
            return self.tr("Projects")
        if index == FOLDERS_TAB:
            return self.tr("Folders")
        return self.tr("Global")

    def _populate(self) -> None:
        app = QApplication.instance()
        app.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.sessions_tab.load(self.workspace.sessions)
            self.projects_selector.clear()
            self._project_labels = []
            for view in self.workspace.projects:
                self._project_labels.append(f"{view.label}\n{view.subtitle}")
                item = QListWidgetItem(self._project_labels[-1])
                item.setToolTip(view.subtitle)
                self.projects_selector.addItem(item)
            self.folders_tab.load(self.workspace.trust_document, self.workspace.pre_trust_roots)
            self.global_tab.load(self.workspace.global_permissions)
            # Capture baselines before selecting a project: _project_selected
            # consults _dirty(), which diffs against the baseline, and an empty
            # baseline would look like unsaved changes and prompt.
            self._capture_baselines()
            if self.workspace.projects:
                self.projects_selector.setCurrentRow(0)
            self._refresh_marks()
            self._update_status(self.tr("Loaded. Edit, then Save. Backups are kept automatically."))
        finally:
            app.restoreOverrideCursor()
        self._log_startup()
        self._flash_load_problems()

    def _log_startup(self) -> None:
        """Record the lock state so the log shows what was open at launch."""
        open_rows = [v.session.session_id for v in self.workspace.sessions if v.locked]
        LOGGER.info(
            "startup: %d session(s), %d open%s",
            len(self.workspace.sessions),
            len(open_rows),
            (": " + ", ".join(open_rows)) if open_rows else "",
        )

    def _flash_load_problems(self) -> None:
        """Report stores that could not be parsed, so the user knows why a tab
        is empty rather than silently showing nothing."""
        problems = self.workspace.problems
        if not problems:
            return
        for problem in problems:
            LOGGER.warning("load problem: %s: %s", problem.path, problem.reason)
        detail = "\n\n".join(f"{problem.path}\n  {problem.reason}" for problem in problems)
        QMessageBox.warning(
            self,
            self.tr("Some files could not be read"),
            self.tr(
                "These files are malformed and were opened as empty; fix them or "
                "close maki before editing:\n\n{detail}"
            ).format(detail=detail),
        )

    def _capture_baselines(self) -> None:
        """Remember each store's serialized content, so Save can skip the ones
        the user did not change (and never rewrite an untouched file).

        A session's baseline is the canonical rendering of its final line (rules
        grouped and sorted the way the editor rebuilds them); the bytes before
        that line never change, so comparing the line is enough — and comparing
        raw file bytes would flag every session whose rules are stored in a
        different order. Stores without a rules line (missing or empty files)
        get no baseline and are therefore never treated as changed.
        """
        self._baseline = {}
        for view in self.workspace.sessions:
            session = view.session
            if not session.has_rules_line:
                continue
            self._baseline[session.path] = sessions.rule_set(session.rules)
        for view in self.workspace.projects:
            file = view.permissions_file
            self._baseline[file.path] = file.to_toml()
        if self.workspace.global_permissions is not None:
            file = self.workspace.global_permissions
            self._baseline[file.path] = file.to_toml()
        if self.workspace.trust_document is not None:
            doc = self.workspace.trust_document
            self._baseline[doc.path] = doc.to_json()
        if self.workspace.pre_trust_roots is not None:
            roots = self.workspace.pre_trust_roots
            self._baseline[roots.path] = roots.to_json()

    def _update_status(self, extra: str) -> None:
        status = self._maki_status()
        prefix = "⚠ " if status.running else "✓ "
        self.status.setText(f"{prefix}{status.message}\n{extra}")

    def _maki_status(self) -> safe_write.MakiStatus:
        """Detect once and cache briefly: a status refresh on every keystroke
        would scan the process table each time."""
        now = time.monotonic()
        if self._maki_cache is None or now - self._maki_checked_at > MAKI_STATUS_TTL:
            self._maki_cache = safe_write.detect_maki()
            self._maki_checked_at = now
        return self._maki_cache

    def _mark_dirty(self) -> None:
        """Push the edited widgets into their model, then refresh marks.

        Commit on every change keeps the model equal to what is on screen, so a
        dirty check is a pure comparison and a switch-away needs no special
        handling. The status line is derived from that comparison rather than
        from this handler having run: opening a session rebuilds its editor and
        fires the same callbacks, and announcing "unsaved changes" there was a
        claim nothing had made true.
        """
        if self._loading or self.sessions_tab.loading:
            return
        self._commit_current()
        self._refresh_marks()
        dirty = sorted(self._session_targets())
        LOGGER.info("change: dirty rows=%s", dirty)
        self._update_status(
            self.tr("Unsaved changes.") if self._dirty() else self.tr("No unsaved changes.")
        )

    def _log_dirty(self, event: str) -> None:
        """Record which rows count as dirty after an event that can move a
        baseline, so a phantom mark can be traced to the step that caused it."""
        LOGGER.info(
            "%s: dirty rows=%s locked=%s",
            event,
            sorted(self._session_targets()),
            [v.session.session_id for v in self.workspace.sessions if v.locked],
        )

    def _dirty(self) -> bool:
        """Whether the visible editors differ from what was loaded.

        Recomputed from the baseline rather than kept as a flag, so discarding
        or reverting an edit clears it and the prompt never fires spuriously.
        """
        return bool(self._collect_targets())

    def _commit_tab(self, index: int) -> None:
        """Park the visible editor's content in its model for the given tab.

        Session and project editors can switch their backing file while showing
        content, so their edits are committed here. Global and folders are single
        files read straight through on save, so they need nothing.
        """
        if index == SESSIONS_TAB:
            self.sessions_tab.commit()
        elif index == PROJECTS_TAB and self._shown_project_row >= 0:
            file = self.projects_tab.collect()
            if file is not None:
                self.workspace.projects[self._shown_project_row].permissions_file = file

    def _commit_current(self) -> None:
        self._commit_tab(self.tabs.currentIndex())

    def closeEvent(self, event) -> None:
        LOGGER.info(
            "close: session rows=%s projects=%s tab=%d",
            sorted(self._session_targets()),
            sorted(self._project_targets()),
            self.tabs.currentIndex(),
        )
        if self._force_close or not self._dirty():
            event.accept()
            return
        answer = QMessageBox.question(
            self,
            self.tr("Unsaved changes"),
            self.tr("You have unsaved changes. Close without saving?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            event.accept()
        else:
            event.ignore()

    def _reload(self) -> None:
        if not self._confirm_discard(self.tr("Reloading all files")):
            return
        self.workspace = workspace.load_workspace()
        self._populate()

    def _pending_names(self) -> list[str]:
        """Human labels of every store holding an unsaved edit, for a warning.

        Naming them is the point: a bare "you have unsaved changes" cannot tell
        a reload over a file the user is editing from one that merely has an
        unrelated edit elsewhere. Derived from ``_changed_stores`` so a store
        that can be saved can never be missing from the list.
        """
        return sorted({target.path.name for _, target in self._changed_stores()})

    def _global_dirty(self) -> bool:
        """Whether the global permissions file differs from its baseline."""
        file = self.workspace.global_permissions
        if file is None or file.path not in self._baseline:
            return False
        return file.to_toml() != self._baseline[file.path]

    def _dirty_names(self, target: safe_write.WriteTarget | None) -> list[str]:
        """The name of ``target`` if it is pending, else empty: asks nothing.

        A per-file reload passes the file it is about to re-read, so it prompts
        only when that file holds an edit and stays silent otherwise.
        """
        if target is None:
            return []
        paths_pending = {t.path for _, t in self._changed_stores()}
        return [target.path.name] if target.path in paths_pending else []

    def _confirm_discard(self, action: str, names: list[str] | None = None) -> bool:
        """Ask before an action that drops unsaved edits; True to proceed.

        {action} completes "Reloading ... will discard them" and {names} is what
        is at risk (default: every dirty store). Reloading re-reads from disk, so
        any edit not yet saved is gone: Close guards this, and every reload must
        too, or a stray click loses work.
        """
        at_risk = self._pending_names() if names is None else names
        if not at_risk:
            return True
        listed = "\n".join(f"  · {name}" for name in at_risk)
        answer = QMessageBox.question(
            self,
            self.tr("Unsaved changes"),
            self.tr("{action} will discard the unsaved changes in:\n{names}").format(
                action=action, names=listed
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _reload_current(self) -> None:
        """Re-read the one file the current tab shows, discarding only its edits.

        The global Reload re-reads every store; this one is for when a single
        file went stale (a running maki rewrote it) and re-reading everything
        would be a pointless disturbance. Folders pairs two files that belong
        together, so both are re-read as a unit, matching how Save writes them.
        """
        index = self.tabs.currentIndex()
        if index == SESSIONS_TAB:
            row = self.sessions_tab.list.currentRow()
            if 0 <= row < len(self.workspace.sessions):
                self._reload_session_row(row)
            return
        if index == PROJECTS_TAB:
            row = self.projects_selector.currentRow()
            if 0 <= row < len(self.workspace.projects):
                self._reload_project_row(row)
            return
        if index == GLOBAL_TAB:
            self._reload_global()
            return
        self._reload_folders()

    def _reload_session_row(self, row: int) -> None:
        view = self.workspace.sessions[row]
        target = self._session_targets().get(row)
        names = self._dirty_names(None if target is None else target[1])
        if not self._confirm_discard(self.tr("Reloading this session"), names):
            return
        was_locked = view.locked
        if not view.reload():
            self._update_status(self.tr("Could not re-read {name}.").format(name=view.label))
            return
        self._rebaseline_session(view)
        # reload=False: view.reload() above already re-read the file, so letting
        # _populate_row reload again would parse it a second time for nothing.
        self.sessions_tab._populate_row(row, reload=False)
        self._refresh_marks()
        LOGGER.info(
            "reload: session=%s locked=%s->%s",
            view.session.session_id,
            was_locked,
            view.locked,
        )
        self._update_status(self.tr("Reloaded {name}.").format(name=view.label))

    def _reload_project_row(self, row: int) -> None:
        view = self.workspace.projects[row]
        target = self._project_targets().get(row)
        names = self._dirty_names(None if target is None else target[1])
        if not self._confirm_discard(self.tr("Reloading this project"), names):
            return
        if not view.reload():
            self._update_status(self.tr("Could not re-read {name}.").format(name=view.label))
            return
        self._baseline[view.permissions_file.path] = view.permissions_file.to_toml()
        if row == self._shown_project_row:
            # load() fires the change callback, which commits the tab back into
            # the model. Doing it inside the loading guard keeps that commit off
            # the freshly read file, or it would restore the discarded edit.
            self._loading = True
            try:
                self.projects_tab.load(view.permissions_file)
            finally:
                self._loading = False
        self._refresh_marks()
        self._update_status(self.tr("Reloaded {name}.").format(name=view.label))

    def _reload_global(self) -> None:
        file = self.workspace.global_permissions
        if file is None:
            return
        name = file.path.name
        pending = self._dirty_names(
            safe_write.WriteTarget(path=file.path) if self._global_dirty() else None
        )
        if not self._confirm_discard(self.tr("Reloading the global file"), pending):
            return
        try:
            self.workspace.global_permissions = permissions.PermissionsFile.load(file.path)
        except (OSError, ValueError):
            self._update_status(self.tr("Could not re-read {name}.").format(name=name))
            return
        reloaded = self.workspace.global_permissions
        self._baseline[reloaded.path] = reloaded.to_toml()
        self.global_tab.load(reloaded)
        self._refresh_marks()
        self._update_status(self.tr("Reloaded {name}.").format(name=name))

    def _reload_folders(self) -> None:
        """Re-read the folder-trust document and the pre-trust roots together."""
        if not self._confirm_discard(self.tr("Reloading the folder trust files")):
            return
        try:
            self.workspace.trust_document = folders.TrustDocument.load(paths.trusted_folders_path())
            self.workspace.pre_trust_roots = folders.PreTrustRoots.load(
                paths.pre_trust_roots_path()
            )
        except (OSError, ValueError):
            self._update_status(self.tr("Could not re-read the folder trust files."))
            return
        for store in (self.workspace.trust_document, self.workspace.pre_trust_roots):
            if store is not None:
                self._baseline[store.path] = store.to_json()
        self.folders_tab.load(self.workspace.trust_document, self.workspace.pre_trust_roots)
        self._refresh_marks()
        self._update_status(self.tr("Reloaded the folder trust files."))

    def _tab_changed(self, index: int) -> None:
        """Refresh tab titles and re-read the global file when it comes into view.

        Global has no selector, so it is only re-read here: a running maki can
        rewrite ``permissions.toml`` when it records a global permission, and a
        stale copy would show (and save over) old rules. Skipped while anything
        is dirty so an unsaved edit is never discarded.
        """
        self._commit_tab(self._active_tab)
        self._active_tab = index
        self._refresh_marks()
        if index != GLOBAL_TAB or self.workspace.global_permissions is None:
            return
        path = self.workspace.global_permissions.path
        if self._global_dirty():
            return
        try:
            self.workspace.global_permissions = permissions.PermissionsFile.load(path)
        except (OSError, ValueError):
            return
        self.global_tab.load(self.workspace.global_permissions)
        # Advance only the global file's baseline. A full recapture would reset
        # every other store's baseline to its (edited) model, silently clearing
        # their dirty marks and losing those edits on save.
        self._baseline[path] = self.workspace.global_permissions.to_toml()

    def _project_selected(self, row: int) -> None:
        if row != self._shown_project_row and self._shown_project_row >= 0:
            self._commit_tab(PROJECTS_TAB)
            self._refresh_marks()
        if row < 0 or row >= len(self.workspace.projects):
            self.projects_tab.load(None)
            self.add_tool_btn.setEnabled(False)
            self.add_mcp_btn.setEnabled(False)
            return
        view = self.workspace.projects[row]
        # Re-read a project file so a change maki made mid-run shows — but only
        # when the incoming project has no pending edit. Reloading a dirty row
        # would discard the edit (as _populate_row already avoids for sessions).
        reloaded = row not in self._project_targets() and view.reload()
        if reloaded:
            self._baseline[view.permissions_file.path] = view.permissions_file.to_toml()
        # load() fires the change callback, which commits the tab into the model.
        # Inside the loading guard that commit cannot rewrite the file just read
        # from disk, which would re-dirty a project nobody edited.
        self._loading = True
        try:
            self.projects_tab.load(view.permissions_file)
        finally:
            self._loading = False
        self._shown_project_row = row
        self.add_tool_btn.setEnabled(True)
        self.add_mcp_btn.setEnabled(True)

    def _add_project_tool(self) -> None:
        name, ok = QInputDialog.getText(
            self, self.tr("Add tool section"), self.tr("Tool name (e.g. write):")
        )
        if not ok or not name.strip():
            return
        error = self.projects_tab.add_tool_section(name.strip())
        if error:
            QMessageBox.warning(self, self.tr("Cannot add section"), error)

    def _add_project_mcp(self) -> None:
        name, ok = QInputDialog.getText(
            self, self.tr("Add MCP server"), self.tr("Server name (e.g. github):")
        )
        if not ok or not name.strip():
            return
        error = self.projects_tab.add_mcp_section(name.strip())
        if error:
            QMessageBox.warning(self, self.tr("Cannot add section"), error)

    def _session_row_dirty(self, row: int) -> bool:
        return row in self._session_targets()

    def _session_pending_edit(self, row: int) -> bool:
        """Whether a row holds an unsaved edit, ignoring its lock state.

        ``_session_row_dirty`` answers "is this a save target", which excludes a
        locked row. The liveness path needs the narrower question — did the user
        change this row? — because a parked edit is still a pending edit, and
        re-baselining it (as a clean row) would silently discard it.
        """
        view = self.workspace.sessions[row]
        session = view.session
        if not session.has_rules_line or session.path not in self._baseline:
            return False
        return sessions.rule_set(session.rules) != self._baseline[session.path]

    def _session_targets(self) -> dict[int, tuple[int, safe_write.WriteTarget]]:
        """Dirty sessions keyed by list row, for saving and row markers.

        A locked session (maki holds it open) is never a target: it cannot be
        saved, and maki rewrites the file while it runs, so the reload that
        refreshes a locked session's view would otherwise show a spurious diff
        against the baseline and mark it dirty with no edit made.
        """
        dirty: dict[int, tuple[int, safe_write.WriteTarget]] = {}
        for row, view in enumerate(self.workspace.sessions):
            if view.locked:
                continue
            session = view.session
            if not session.has_rules_line:
                continue
            if session.path not in self._baseline:
                continue
            if sessions.rule_set(session.rules) != self._baseline[session.path]:
                dirty[row] = (
                    SESSIONS_TAB,
                    safe_write.WriteTarget(path=session.path, shared=False, writer=session.write),
                )
        return dirty

    def _project_targets(self) -> dict[int, tuple[int, safe_write.WriteTarget]]:
        """Dirty project files keyed by selector row."""
        dirty: dict[int, tuple[int, safe_write.WriteTarget]] = {}
        for row, view in enumerate(self.workspace.projects):
            file = view.permissions_file
            if file.path not in self._baseline:
                continue
            if file.to_toml() != self._baseline[file.path]:
                dirty[row] = (
                    PROJECTS_TAB,
                    safe_write.WriteTarget(path=file.path, shared=True),
                )
        return dirty

    def _changed_stores(self) -> list[tuple[int, safe_write.WriteTarget]]:
        """Every store whose model differs from the baseline.

        Each entry pairs the tab index that owns the store with its write
        target, so Save can write them and the tab titles can mark them. Edits
        live in the model (committed on switch), so the visible editor is parked
        first and then every model is compared. Comparing against the loaded
        baseline keeps a Save from rewriting (and re-backing-up) files the user
        never touched, and from creating an empty ``permissions.toml`` in a
        project that had none. A store with no baseline (a missing or empty
        file) is never dirty: there is nothing to compare against.
        """
        changed: list[tuple[int, safe_write.WriteTarget]] = []

        def record(tab: int, path, content: str, shared: bool = True) -> None:
            if path in self._baseline and content != self._baseline[path]:
                changed.append(
                    (tab, safe_write.WriteTarget(path=path, content=content, shared=shared))
                )

        changed.extend(self._session_targets().values())
        changed.extend(self._project_targets().values())

        global_file = self.global_tab.collect()
        if global_file is not None:
            record(GLOBAL_TAB, global_file.path, global_file.to_toml())

        doc = self.workspace.trust_document
        if doc is not None:
            record(2, doc.path, doc.to_json())

        roots = self.workspace.pre_trust_roots
        if roots is not None:
            record(2, roots.path, roots.to_json())

        return changed

    def _collect_targets(self) -> list[safe_write.WriteTarget]:
        return [target for _, target in self._changed_stores()]

    def _current_targets(self) -> list[safe_write.WriteTarget]:
        """Write targets for the active tab's current selection only.

        Save is per file: editing several sessions leaves them all dirty (each
        marked in its list), but a Save writes just the one in view. Folders
        holds two files that belong together, so both are written as a unit.
        """
        index = self.tabs.currentIndex()
        self._commit_current()
        if index == SESSIONS_TAB:
            row = self.sessions_tab.list.currentRow()
            entry = self._session_targets().get(row)
            return [entry[1]] if entry else []
        if index == PROJECTS_TAB:
            row = self.projects_selector.currentRow()
            entry = self._project_targets().get(row)
            return [entry[1]] if entry else []
        if index == GLOBAL_TAB:
            if self.workspace.global_permissions is None:
                return []
            file = self.workspace.global_permissions
            if file.path in self._baseline and file.to_toml() != self._baseline[file.path]:
                return [safe_write.WriteTarget(path=file.path, content=file.to_toml())]
            return []
        return [target for tab, target in self._changed_stores() if tab == self.tabs.currentIndex()]

    def _serialized(self, path) -> str | None:
        """The model's current serialized form for ``path``, if it owns it.

        Used to advance a file's baseline right after saving it, without
        touching other stores' baselines.
        """
        for view in self.workspace.sessions:
            if view.session.path == path and view.session.has_rules_line:
                return sessions.rule_set(view.session.rules)
        for view in self.workspace.projects:
            if view.permissions_file.path == path:
                return view.permissions_file.to_toml()
        if self.workspace.global_permissions is not None:
            if self.workspace.global_permissions.path == path:
                return self.workspace.global_permissions.to_toml()
        if self.workspace.trust_document is not None:
            if self.workspace.trust_document.path == path:
                return self.workspace.trust_document.to_json()
        if self.workspace.pre_trust_roots is not None:
            if self.workspace.pre_trust_roots.path == path:
                return self.workspace.pre_trust_roots.to_json()
        return None

    def _refresh_marks(self) -> None:
        """Mark tabs, sessions and projects that hold unsaved changes.

        A file stays dirty until its own Save, so the tab title gets ``*`` and
        the list row gets the same mark, letting the user see which of several
        edited sessions still needs writing.
        """
        dirty_sessions = self._session_targets()
        dirty_projects = self._project_targets()
        for row in range(self.sessions_tab.list.count()):
            item = self.sessions_tab.list.item(row)
            base = self.sessions_tab.row_label(row)
            item.setText(base + (DIRTY_MARK if row in dirty_sessions else ""))
        for row in range(self.projects_selector.count()):
            item = self.projects_selector.item(row)
            mark = DIRTY_MARK if row in dirty_projects else ""
            item.setText(self._project_labels[row] + mark)
        dirty_tabs = {index for index, _ in self._changed_stores()}
        for index in range(len(TAB_TITLES)):
            mark = DIRTY_MARK if index in dirty_tabs else ""
            self.tabs.setTabText(index, self._tab_title(index) + mark)

    def _refresh_liveness(self) -> None:
        """Re-probe maki and the visible sessions, then reflect any change.

        A lock can appear (maki opens a session we are viewing) or vanish (maki
        exits) with nothing here touched, so the view would lie until Save. This
        is the only periodic work; it is idle-safe (a non-blocking lock probe and
        a brief process scan) and driven by the poll timer and window focus.
        """
        self._maki_cache = None
        previous = self._maki_cache_prev
        self._maki_cache_prev = self._maki_status().running
        locked_rows = self._probe_visible_locks()
        if locked_rows:
            self._apply_lock_changes(locked_rows)
        if previous is not None and previous != self._maki_cache_prev:
            state = self.tr("maki started") if self._maki_cache_prev else self.tr("maki exited")
            self._update_status(state)
        self._refresh_marks()

    def _probe_visible_locks(self) -> set[int]:
        """Rows whose live lock state differs from the cached view.

        Scoped to the visible rows only: the active row is always re-probed (it
        is what the user is editing), and the rest is whatever is on screen.
        Probing all sessions would touch every lock file each tick for rows the
        user cannot see; those are caught when they scroll into view, because
        selecting a row re-probes it.
        """
        sessions_dir = paths.sessions_dir()
        changed: set[int] = set()
        for row in self._visible_session_rows():
            view = self.workspace.sessions[row]
            locked = sessions.is_locked(sessions_dir, view.session.session_id)
            if locked != view.locked:
                changed.add(row)
        return changed

    def _visible_session_rows(self) -> list[int]:
        """The active session row plus every row in the viewport.

        No scroll hook: an off-screen lock is caught when its row is selected,
        which re-probes it, so the cheap viewport range is enough and no
        scrollbar signal is needed.
        """
        if not self.workspace.sessions:
            return []
        row = self.sessions_tab.list.currentRow()
        if row < 0:
            row = self.sessions_tab._shown
        rows = {row} if row >= 0 else set()
        count = self.sessions_tab.list.count()
        for screen_row in range(count):
            item = self.sessions_tab.list.item(screen_row)
            if (
                self.sessions_tab.list.viewport()
                .rect()
                .intersects(self.sessions_tab.list.visualItemRect(item))
            ):
                rows.add(screen_row)
        return sorted(rows)

    def _apply_lock_changes(self, rows: set[int]) -> None:
        """Flip the changed rows' lock state and reflect it in the view.

        A row that just became locked is shown read-only and its ``+ Add`` is
        disabled, matching a session that was already open when it loaded; its
        content is refreshed from disk first, unless it holds an unsaved edit.
        An edit is left alone (reloading would drop it): it is parked, marked,
        and simply stops being a save target. The active row's editor is
        re-populated so the change is visible at once.

        Every row reloaded while clean is re-baselined: maki rewrites a session's
        rules while it holds it open, so the copy reloaded on the lock *or* the
        unlock differs from the baseline taken at load. Without advancing it, the
        unlock would compare maki's own rewrite against the old baseline and mark
        a session the user never touched as dirty.
        """
        LOGGER.info("lock change: rows=%s", sorted(rows))
        sessions_dir = paths.sessions_dir()
        shown = self.sessions_tab._shown
        parked: set[int] = set()
        rebaselined: set[int] = set()
        for row in rows:
            view = self.workspace.sessions[row]
            now_locked = sessions.is_locked(sessions_dir, view.session.session_id)
            pending = self._session_pending_edit(row)
            if now_locked and pending:
                self._park_on_lock(row)
                parked.add(row)
            elif not pending:
                if row != shown:
                    view.reload()
                rebaselined.add(row)
            LOGGER.info(
                "lock change: row=%d id=%s locked=%s pending=%s",
                row,
                view.session.session_id,
                now_locked,
                pending,
            )
            view.locked = now_locked
        if shown in rows and shown not in parked:
            self.sessions_tab._populate_row(shown)
        elif shown in parked:
            # Re-populating a parked row would reload it: the lock excludes it
            # from the dirty set, so _populate_row's guard reads it as clean and
            # re-reads the file, dropping the edit. Flip the editor in place.
            self.sessions_tab.set_read_only(True)
        for row in rebaselined:
            self._rebaseline_session(self.workspace.sessions[row])
        self._log_dirty("lock change")

    def _rebaseline_session(self, view: workspace.SessionView, *, sync: bool = True) -> None:
        """Adopt a reloaded session's rules as its baseline.

        A reload that is not a user edit (maki rewriting a session it holds) must
        move the baseline with it, or the next dirty check reads that rewrite as
        an edit. Mirrors the session branch of ``_capture_baselines``.

        {sync} rewrites the row text, which the lock path needs because the lock
        glyph changed. The populate path passes ``False``: it runs while a row is
        being opened and its labels are about to be refreshed with the dirty
        marks, so a plain rewrite here would briefly drop another row's asterisk.
        """
        session = view.session
        if session.has_rules_line:
            self._baseline[session.path] = sessions.rule_set(session.rules)
        if sync:
            self.sessions_tab.sync_labels()

    def _park_on_lock(self, row: int) -> None:
        """Report a dirty row that just became locked, keeping the edit.

        The edit is not discarded: it stays in the model and is dropped from the
        save targets, so it is lost only if the user reloads. The status line
        says so with the warning prefix.
        """
        label = self.workspace.sessions[row].session.session_id
        LOGGER.warning("session locked with unsaved changes: %s", label)
        self._update_status(
            self.tr("Session {id} is now open in maki; unsaved changes are parked.").format(
                id=label
            )
        )

    def _drop_locked_sessions(
        self, targets: list[safe_write.WriteTarget]
    ) -> list[safe_write.WriteTarget]:
        """Remove session targets that a running maki holds open.

        Writing an open session risks maki clobbering it on exit, so a locked
        session is never saved; the user is told which ones were skipped. A
        session's id is its file stem, which is also the lock file name, so we
        probe live (not the cached view) to catch one that opened since load.
        """
        locked = [
            t
            for t in targets
            if not t.shared and sessions.is_locked(paths.sessions_dir(), t.path.stem)
        ]
        if not locked:
            return targets
        LOGGER.warning("save skipped open session(s): %s", ", ".join(t.path.stem for t in locked))
        names = "\n".join(f"  · {t.path.stem}" for t in locked)
        QMessageBox.information(
            self,
            self.tr("Session is open in maki"),
            self.tr(
                "These sessions are open in maki and were not saved:\n{names}\n\n"
                "Close the session in maki, then save again."
            ).format(names=names),
        )
        locked_paths = {t.path for t in locked}
        return [t for t in targets if t.path not in locked_paths]

    def _review_changes(self) -> changes.ChangeList | None:
        """What the pending save of the current view would change, or None.

        Built from the file on disk rather than the baseline: a running maki can
        have rewritten the file since it was loaded, and the dialog must describe
        the write that is actually about to happen.
        """
        index = self.tabs.currentIndex()
        if index == SESSIONS_TAB:
            row = self.sessions_tab.list.currentRow()
            if not 0 <= row < len(self.workspace.sessions):
                return None
            session = self.workspace.sessions[row].session
            if not session.has_rules_line:
                return None
            review = changes.session_changes(session.path, session.rules)
        elif index == PROJECTS_TAB:
            row = self.projects_selector.currentRow()
            if not 0 <= row < len(self.workspace.projects):
                return None
            file = self.workspace.projects[row].permissions_file
            review = changes.permissions_changes(file.path, file)
        elif index == GLOBAL_TAB:
            file = self.workspace.global_permissions
            if file is None:
                return None
            review = changes.permissions_changes(file.path, file)
        else:
            doc = self.workspace.trust_document
            if doc is None:
                return None
            review = changes.folder_changes(doc.path, doc, self.workspace.pre_trust_roots)
        return review

    def _confirm_save(self, review: changes.ChangeList) -> bool:
        """Ask before writing, when there is something to ask about.

        A session file is not held open by maki, so writing it needs no
        explanation beyond the change list and the lock check already done. A
        shared file cannot be written at all while maki runs, so asking would
        only be a question whose answer is ignored: it is refused instead.
        """
        if all(not target.shared for target in self._current_targets()):
            return True
        return SaveDialog(self, review).exec() == QDialog.DialogCode.Accepted

    def _refuse_shared_write(
        self, targets: list[safe_write.WriteTarget], blocked: list[safe_write.WriteTarget], status
    ) -> None:
        """Tell the user a shared file must wait for maki to close."""
        LOGGER.warning("save blocked, maki running: %s", ", ".join(str(t.path) for t in blocked))
        names = "\n".join(f"  · {t.path.name}" for t in blocked)
        QMessageBox.information(
            self,
            self.tr("maki is running"),
            self.tr("Cannot write {names} while maki is running. Close it and retry.").format(
                names=names
            ),
        )
        self._update_status(self.tr("Not saved: maki is running."))

    def _save(self) -> None:
        targets = self._current_targets()
        if not targets:
            self._update_status(self.tr("Nothing to save in this view."))
            return

        LOGGER.info("save: %d target(s)", len(targets))
        targets = self._drop_locked_sessions(targets)
        if not targets:
            LOGGER.info("save: nothing left, target open in maki")
            self._update_status(self.tr("Nothing saved: file is open in maki."))
            return

        review = self._review_changes()
        if review is not None and review.empty:
            self._update_status(self.tr("Nothing to save: this view matches the file on disk."))
            return
        if review is not None and not self._confirm_save(review):
            LOGGER.info("save cancelled at review: %s", review.path)
            self._update_status(self.tr("Save cancelled; your changes are still pending."))
            return

        status = safe_write.detect_maki()
        blocked = safe_write.blocked_targets(targets, status)
        if blocked:
            self._refuse_shared_write(targets, blocked, status)
            return

        saved = {t.path for t in targets}
        try:
            result = safe_write.write_targets(targets)
        except OSError as exc:
            LOGGER.error("save failed: %s", exc)
            QMessageBox.critical(self, self.tr("Save failed"), str(exc))
            return
        LOGGER.info("save ok: %s", result.describe())
        # Only the saved files' baselines move: reloading the whole workspace
        # would drop the unsaved edits still parked in other files.
        for path in saved:
            serialized = self._serialized(path)
            if serialized is not None:
                self._baseline[path] = serialized
        self._refresh_marks()
        self._update_status(result.describe())
