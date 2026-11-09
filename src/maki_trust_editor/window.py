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

import logging
import re
import time

from qtpy.QtCore import Qt
from qtpy.QtGui import QFontMetrics, QKeySequence, QShortcut
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

from . import paths, permissions, safe_write, sessions, workspace
from .permissions import EFFECT_ALLOW, McpServerRules, ToolRules
from .widgets import ScopedHolder, ScopeRulesEditor

MAKI_STATUS_TTL = 2.0
TOOL_NAME_RE = re.compile(r"^\w[\w-]*$")
DIRTY_MARK = " *"
# Tab labels in addTab order, so titles can be rebuilt with the dirty marker
# without repeating the strings.
TAB_TITLES = ("Sessions", "Projects", "Folders", "Global")
SESSIONS_TAB = TAB_TITLES.index("Sessions")
PROJECTS_TAB = TAB_TITLES.index("Projects")
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

        layout = QVBoxLayout(self)
        self.path_label = QLabel(empty_hint)
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path_label.setStyleSheet("font-family: monospace; color: #888;")
        layout.addWidget(self.path_label)

        self.scoped = ScopedHolder("Rules")
        layout.addWidget(self.scoped)
        self._editors: list[tuple[str, ScopeRulesEditor, bool]] = []

    def load(self, file: permissions.PermissionsFile | None) -> None:
        self._file = file
        self.scoped.clear_editors()
        self._editors.clear()
        if file is None:
            self.path_label.setText(self._empty_hint)
            return
        state = "exists" if file.exists else "will be created on save"
        self.path_label.setText(f"file: {file.path}\n({state})")
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
            return "Select a project first."
        if not TOOL_NAME_RE.match(name):
            return (
                "Names must start with a letter, digit or underscore and contain "
                "only letters, digits, underscores and hyphens."
            )
        if name in self._file.tools:
            return f"A tool section named '{name}' already exists."
        if name in self._file.mcp:
            return f"An MCP section named '{name}' already exists."
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
    def __init__(self, on_rules_changed, commit, is_dirty) -> None:
        super().__init__()
        self._views: list[workspace.SessionView] = []
        self._current: workspace.Session | None = None
        self._commit = commit
        self._is_dirty = is_dirty

        layout = QHBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._shown = -1
        self.loading = False

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("Sessions"))
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._selected)
        left_layout.addWidget(self.list)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.file_label = QLabel("file: (none)")
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.file_label.setStyleSheet("font-family: monospace; color: #888;")
        self.file_label.setWordWrap(True)
        right_layout.addWidget(self.file_label)
        self.header = QLabel("Select a session")
        self.header.setWordWrap(True)
        right_layout.addWidget(self.header)
        self.scoped = ScopedHolder("Session rules (allow/deny per scope)")
        right_layout.addWidget(self.scoped)
        self._editors: list[tuple[str, ScopeRulesEditor]] = []
        self._on_rules_changed = on_rules_changed

        self._add_button = QPushButton("+ Add tool group")
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
            self._labels.append(f"{_lock_prefix(view.locked)}{view.label}\n{view.subtitle}")
            item = QListWidgetItem(self._labels[-1])
            item.setToolTip(("open in maki\n" if view.locked else "") + view.subtitle)
            self.list.addItem(item)
        self.list.blockSignals(False)
        self._selected(self.list.currentRow())

    def row_label(self, row: int) -> str:
        return self._labels[row]

    def _selected(self, row: int) -> None:
        if row != self._shown:
            # Park the outgoing session's edits in its model so they survive the
            # switch (dirty persists across clicks) instead of being dropped.
            self._commit()
        self._populate_row(row)

    def _populate_row(self, row: int) -> None:
        self._shown = row
        self.loading = True
        self.scoped.clear_editors()
        self._editors.clear()
        if row < 0 or row >= len(self._views):
            self._current = None
            self._add_button.setEnabled(False)
            self.file_label.setText("file: (none)")
            self.header.setText("Select a session")
            self.loading = False
            return
        view = self._views[row]
        # Re-read before showing: a live session grows while maki runs, so the
        # copy loaded at startup has a stale last-line offset. Loading fresh
        # keeps the rules (and the offset a save writes from) current. A row
        # with a pending edit is left alone: reloading would drop it.
        if not self._is_dirty(row):
            view.reload()
        session = view.session
        self._current = session
        self.file_label.setText(f"file: {session.path}")
        self.header.setText(
            f"{session.session_id}\ncwd: {session.cwd}\nmodel: {session.model}\n"
            f"{len(session.rules)} rule(s)"
        )
        for group in sessions.ToolGroup.group(session.rules):
            editor = ScopeRulesEditor(self._on_rules_changed)
            editor.set_section(None, group.all_effect)
            for scope, effect in group.scopes:
                editor.add_row(scope, effect)
            self.scoped.add_editor(group.tool, editor)
            self._editors.append((group.tool, editor))
        # A session maki holds open is shown but not editable: any write would
        # race with maki re-appending to the same file on exit.
        read_only = view.locked
        self.scoped.set_read_only(read_only)
        self._add_button.setEnabled(not read_only)
        self.scoped.refresh()
        self.loading = False

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
        name, ok = QInputDialog.getText(self, "Add tool group", "Tool name (e.g. bash):")
        name = name.strip()
        if not ok or not name:
            return
        if not TOOL_NAME_RE.match(name):
            QMessageBox.warning(
                self,
                "Cannot add group",
                "Names must start with a letter, digit or underscore and contain "
                "only letters, digits, underscores and hyphens.",
            )
            return
        if any(tool == name for tool, _ in self._editors):
            QMessageBox.warning(self, "Cannot add group", f"A group named '{name}' already exists.")
            return
        editor = ScopeRulesEditor(self._on_rules_changed)
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


class FolderDialog(QDialog):
    def __init__(self, parent, kind="trusted", path="", files=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Folder Trust")
        layout = QFormLayout(self)
        self.path_input = QLineEdit(path)
        layout.addRow("Path:", self.path_input)
        self.files_input = QLineEdit(", ".join(files) if files else "")
        layout.addRow("Gated files (comma-separated):", self.files_input)
        self.trusted_radio = QRadioButton("Trusted")
        self.rejected_radio = QRadioButton("Rejected")
        if kind == "trusted":
            self.trusted_radio.setChecked(True)
        else:
            self.rejected_radio.setChecked(True)
        layout.addRow("Type:", self.trusted_radio)
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
        self.file_label = QLabel("file: (none)")
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.file_label.setStyleSheet("font-family: monospace; color: #888;")
        self.file_label.setWordWrap(True)
        layout.addWidget(self.file_label)
        layout.addWidget(
            QLabel(
                "Folder trust. Add, edit or remove a trusted folder or a rejected "
                "path; changes are written on Save."
            )
        )
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._edit_item)
        layout.addWidget(self.list)
        buttons = QHBoxLayout()
        add_btn = QPushButton("+ Add folder")
        add_btn.clicked.connect(self._show_add_dialog)
        buttons.addWidget(add_btn)
        remove = QPushButton("Remove selected")
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
        group = QGroupBox("Pre-trust roots (frozen snapshot)")
        box = QVBoxLayout(group)
        self.pre_trust_label = QLabel("file: (none)")
        self.pre_trust_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.pre_trust_label.setStyleSheet("font-family: monospace; color: #888;")
        self.pre_trust_label.setWordWrap(True)
        box.addWidget(self.pre_trust_label)
        box.addWidget(
            QLabel(
                "Project roots that predate folder trust and are grandfathered at "
                "startup. Add or remove paths; changes are written on Save."
            )
        )
        self.pre_trust_list = QListWidget()
        box.addWidget(self.pre_trust_list)
        row = QHBoxLayout()
        add = QPushButton("+ Add root")
        add.clicked.connect(self._add_pre_trust_root)
        row.addWidget(add)
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self._remove_pre_trust_root)
        row.addWidget(remove)
        row.addStretch(1)
        box.addLayout(row)
        return group

    def load(self, doc, pre_trust=None) -> None:
        self._doc = doc
        if doc is not None:
            state = "exists" if doc.exists else "will be created on save"
            self.file_label.setText(f"file: {doc.path}\n({state})")
        else:
            self.file_label.setText("file: (none)")
        self._pre_trust = pre_trust
        if pre_trust is not None:
            state = "exists" if pre_trust.exists else "will be created on save"
            self.pre_trust_label.setText(f"file: {pre_trust.path}\n({state})")
        else:
            self.pre_trust_label.setText("file: (none)")
        self._refresh()

    def _refresh(self) -> None:
        self.list.clear()
        self.pre_trust_list.clear()
        if self._doc is not None:
            for entry in self._doc.folders:
                item = QListWidgetItem(
                    f"[trusted] {entry.path}\n  gated files: {', '.join(entry.files) or 'none'}"
                )
                item.setData(Qt.ItemDataRole.UserRole, ("trusted", entry.path))
                self.list.addItem(item)
            for path in self._doc.rejected:
                item = QListWidgetItem(f"[rejected] {path}")
                item.setData(Qt.ItemDataRole.UserRole, ("rejected", path))
                self.list.addItem(item)
        if self._pre_trust is not None:
            for root in self._pre_trust.roots:
                self.pre_trust_list.addItem(root)

    def _add_pre_trust_root(self) -> None:
        if self._pre_trust is None:
            return
        directory = QFileDialog.getExistingDirectory(self, "Add pre-trust root")
        if not directory:
            return
        if not self._pre_trust.add(directory):
            QMessageBox.information(self, "Already present", f"{directory} is already listed.")
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

        self._last_project_row = -1
        self._active_tab = SESSIONS_TAB
        self._loading = False
        self._baseline: dict = {}
        self._maki_cache = None
        self._maki_checked_at = 0.0
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
        self.sessions_tab = SessionsTab(self._mark_dirty, self._mark_dirty, self._session_row_dirty)
        self.global_tab = PermissionsTab("No global permissions file yet.", self._mark_dirty)
        self.projects_tab = PermissionsTab("Select a project.", self._mark_dirty)
        self.folders_tab = FoldersTab(self._mark_dirty)

        # The projects tab pairs a selector with the permissions editor.
        self.projects_selector = QListWidget()
        self.projects_selector.setMaximumWidth(320)
        self.projects_selector.currentRowChanged.connect(self._project_selected)
        projects_container = QWidget()
        pc_layout = QHBoxLayout(projects_container)
        pc_layout.addWidget(self.projects_selector)
        pc_layout.addWidget(self.projects_tab, 1)

        self.tabs.addTab(self.sessions_tab, "Sessions")
        self.tabs.addTab(projects_container, "Projects")
        self.tabs.addTab(self.folders_tab, "Folders")
        self.tabs.addTab(self.global_tab, "Global")
        self.tabs.currentChanged.connect(self._tab_changed)
        layout.addWidget(self.tabs)

        bar = QHBoxLayout()
        save = QPushButton("Save")
        save.clicked.connect(self._save)
        reload_btn = QPushButton("Reload")
        reload_btn.clicked.connect(self._reload)
        self.add_tool_btn = QPushButton("+ Tool section (project)")
        self.add_tool_btn.clicked.connect(self._add_project_tool)
        self.add_mcp_btn = QPushButton("+ MCP server (project)")
        self.add_mcp_btn.clicked.connect(self._add_project_mcp)
        bar.addWidget(reload_btn)
        bar.addWidget(self.add_tool_btn)
        bar.addWidget(self.add_mcp_btn)
        bar.addStretch(1)
        bar.addWidget(save)
        layout.addLayout(bar)

        self.setCentralWidget(central)
        self._setup_shortcuts()
        self._populate()

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._save)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self._reload)

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
            self._update_status("Loaded. Edit, then Save. Backups are kept automatically.")
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
            "Some files could not be read",
            "These files are malformed and were opened as empty; fix them or "
            f"close maki before editing:\n\n{detail}",
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
            session.rules = sessions.canonical_rules(session.rules)
            self._baseline[session.path] = session.rules_line()
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

        Committing on every edit keeps the model equal to what is on screen, so
        a dirty check is a pure comparison and a switch-away needs no special
        handling: the edit is already parked where Save will find it.
        """
        if self._loading or self.sessions_tab.loading:
            return
        self._commit_current()
        self._refresh_marks()
        self._update_status("Unsaved changes.")

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
        elif index == PROJECTS_TAB and self.projects_selector.currentRow() >= 0:
            file = self.projects_tab.collect()
            if file is not None:
                self.workspace.projects[self.projects_selector.currentRow()].permissions_file = file

    def _commit_current(self) -> None:
        self._commit_tab(self.tabs.currentIndex())

    def closeEvent(self, event) -> None:
        if self._force_close or not self._dirty():
            event.accept()
            return
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            "You have unsaved changes. Close without saving?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            event.accept()
        else:
            event.ignore()

    def _reload(self) -> None:
        self.workspace = workspace.load_workspace()
        self._populate()

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
        if path in self._baseline and (
            self.workspace.global_permissions.to_toml() != self._baseline[path]
        ):
            return
        try:
            self.workspace.global_permissions = permissions.PermissionsFile.load(path)
        except (OSError, ValueError):
            return
        self.global_tab.load(self.workspace.global_permissions)
        self._capture_baselines()

    def _project_selected(self, row: int) -> None:
        if row != self._last_project_row and self._last_project_row >= 0:
            self._commit_tab(PROJECTS_TAB)
            self._refresh_marks()
        if row < 0 or row >= len(self.workspace.projects):
            self.projects_tab.load(None)
            self.add_tool_btn.setEnabled(False)
            self.add_mcp_btn.setEnabled(False)
            return
        view = self.workspace.projects[row]
        view.reload()
        self.projects_tab.load(view.permissions_file)
        self._last_project_row = row
        self.add_tool_btn.setEnabled(True)
        self.add_mcp_btn.setEnabled(True)

    def _add_project_tool(self) -> None:
        name, ok = QInputDialog.getText(self, "Add tool section", "Tool name (e.g. write):")
        if not ok or not name.strip():
            return
        error = self.projects_tab.add_tool_section(name.strip())
        if error:
            QMessageBox.warning(self, "Cannot add section", error)

    def _add_project_mcp(self) -> None:
        name, ok = QInputDialog.getText(self, "Add MCP server", "Server name (e.g. github):")
        if not ok or not name.strip():
            return
        error = self.projects_tab.add_mcp_section(name.strip())
        if error:
            QMessageBox.warning(self, "Cannot add section", error)

    def _session_row_dirty(self, row: int) -> bool:
        return row in self._session_targets()

    def _session_targets(self) -> dict[int, tuple[int, safe_write.WriteTarget]]:
        """Dirty sessions keyed by list row, for saving and row markers."""
        dirty: dict[int, tuple[int, safe_write.WriteTarget]] = {}
        for row, view in enumerate(self.workspace.sessions):
            session = view.session
            if not session.has_rules_line:
                continue
            if session.path not in self._baseline:
                continue
            if session.rules_line() != self._baseline[session.path]:
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
                return view.session.rules_line()
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
        for index, title in enumerate(TAB_TITLES):
            self.tabs.setTabText(index, title + (DIRTY_MARK if index in dirty_tabs else ""))

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
            "Session is open in maki",
            f"These sessions are open in maki and were not saved:\n{names}\n\n"
            "Close the session in maki, then save again.",
        )
        locked_paths = {t.path for t in locked}
        return [t for t in targets if t.path not in locked_paths]

    def _save(self) -> None:
        targets = self._current_targets()
        if not targets:
            self._update_status("Nothing to save in this view.")
            return

        LOGGER.info("save: %d target(s)", len(targets))
        targets = self._drop_locked_sessions(targets)
        if not targets:
            LOGGER.info("save: nothing left, target open in maki")
            self._update_status("Nothing saved: file is open in maki.")
            return

        status = safe_write.detect_maki()
        if status.running:
            blocked = safe_write.blocked_targets(targets, status)
            names = "\n".join(f"  · {t.path}" for t in blocked) or (
                "  (none — only session writes pending)"
            )
            detail = (
                f"maki is running (pid {', '.join(str(p) for p in status.pids)}).\n\n"
                f"Shared files that must wait until maki closes:\n{names}\n\n"
                "Session files are not held open by maki, so they can be written now, "
                "but a running session of the same file may re-append on exit.\n\n"
                "Write the session files only and skip the shared ones?"
            )
            choice = QMessageBox.question(
                self,
                "maki is running",
                detail,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return
            if blocked:
                blocked_paths = {t.path for t in blocked}
                targets = [t for t in targets if t.path not in blocked_paths]
            if not targets:
                self._update_status("No writable targets; close maki and retry.")
                return

        saved = {t.path for t in targets}
        try:
            result = safe_write.save_all(targets)
        except OSError as exc:
            LOGGER.error("save failed: %s", exc)
            QMessageBox.critical(self, "Save failed", str(exc))
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
