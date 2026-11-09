"""Gather every store into the view models the UI renders.

This module is the single place that touches the filesystem for reads, so the
widgets stay free of path logic and the load path is easy to test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import folders, paths, permissions, sessions


@dataclass
class SessionView:
    session: sessions.Session
    locked: bool = False

    def reload(self) -> bool:
        """Re-read the session's header, last line and lock state.

        A session file grows while maki runs (and is rewritten on exit), so the
        bytes loaded at startup go stale: the recorded last-line offset would
        point into the middle of the file. Re-reading on every selection keeps
        the shown rules and the offset a save writes from correct. Returns
        ``False`` when the file is gone or unreadable, leaving prior state.
        """
        try:
            loaded = sessions.Session.load(self.session.path)
        except (OSError, ValueError):
            return False
        self.session = loaded
        self.locked = sessions.is_locked(paths.sessions_dir(), loaded.session_id)
        return True

    @property
    def label(self) -> str:
        return self.session.title.strip() or "(untitled)"

    @property
    def subtitle(self) -> str:
        return (
            f"{self.session.session_id}  ·  {self.session.cwd}"
            f"  ·  {len(self.session.rules)} rules"
        )


@dataclass
class ProjectView:
    root: Path
    permissions_file: permissions.PermissionsFile
    exists: bool

    def reload(self) -> bool:
        """Re-read ``.maki/permissions.toml`` so a change maki made mid-run shows.

        The file is rewritten when maki records a project-scoped permission, so
        the copy loaded at startup can be stale. Returns ``False`` (keeping prior
        state) when the file is unreadable.
        """
        path = paths.project_permissions_path(self.root)
        try:
            self.permissions_file = permissions.PermissionsFile.load(path)
        except (OSError, ValueError):
            return False
        self.exists = path.is_file()
        return True

    @property
    def label(self) -> str:
        return self.root.name

    @property
    def subtitle(self) -> str:
        state = "has .maki/permissions.toml" if self.exists else "no project permissions file"
        return f"{self.root}  ·  {state}"


@dataclass
class FolderView:
    path: str
    files: list[str]
    rejected: bool = False

    @property
    def label(self) -> str:
        name = Path(self.path).name or self.path
        return f"{name}  (rejected)" if self.rejected else name

    @property
    def subtitle(self) -> str:
        if self.rejected:
            return f"{self.path}  ·  rejected"
        files = ", ".join(self.files) if self.files else "no gated files"
        return f"{self.path}  ·  {files}"


@dataclass
class LoadProblem:
    """A store that could not be parsed on load, with where and why.

    We keep the app loading rather than crashing on one bad file: the store
    falls back to empty and the window flashes this as a popup.
    """

    path: Path
    reason: str


@dataclass
class Workspace:
    """Everything the UI shows."""

    sessions: list[SessionView] = field(default_factory=list)
    projects: list[ProjectView] = field(default_factory=list)
    folders: list[FolderView] = field(default_factory=list)
    global_permissions: permissions.PermissionsFile | None = None
    trust_document: folders.TrustDocument | None = None
    pre_trust_roots: folders.PreTrustRoots | None = None
    problems: list[LoadProblem] = field(default_factory=list)


def _discover_projects(workspace: Workspace) -> None:
    seen: set[Path] = set()
    for view in workspace.sessions:
        cwd = view.session.cwd
        if not cwd:
            continue
        root = paths.project_root_of(cwd)
        if root in seen:
            continue
        seen.add(root)
        perm_path = paths.project_permissions_path(root)
        workspace.projects.append(
            ProjectView(
                root=root,
                permissions_file=_load_or_problem(
                    workspace,
                    perm_path,
                    permissions.PermissionsFile.load,
                    permissions.PermissionsFile(perm_path),
                ),
                exists=perm_path.is_file(),
            )
        )
    workspace.projects.sort(key=lambda p: p.label.lower())


def _load_or_problem(workspace: Workspace, path: Path, loader, fallback):
    """Run ``loader(path)``; on a parse error record a problem and use fallback."""
    try:
        return loader(path)
    except (OSError, ValueError) as exc:
        workspace.problems.append(LoadProblem(path=path, reason=str(exc)))
        return fallback


def load_workspace() -> Workspace:
    workspace = Workspace()

    for path in sessions.discover_sessions(paths.sessions_dir()):
        view = SessionView(session=sessions.Session(path=path))
        if view.reload():
            workspace.sessions.append(view)

    global_path = paths.global_permissions_path()
    workspace.global_permissions = _load_or_problem(
        workspace,
        global_path,
        permissions.PermissionsFile.load,
        permissions.PermissionsFile(global_path),
    )
    trust_path = paths.trusted_folders_path()
    workspace.trust_document = _load_or_problem(
        workspace, trust_path, folders.TrustDocument.load, folders.TrustDocument(path=trust_path)
    )
    pre_trust_path = paths.pre_trust_roots_path()
    workspace.pre_trust_roots = _load_or_problem(
        workspace,
        pre_trust_path,
        folders.PreTrustRoots.load,
        folders.PreTrustRoots(path=pre_trust_path),
    )

    for entry in workspace.trust_document.folders:
        workspace.folders.append(FolderView(path=entry.path, files=list(entry.files)))
    for rejected in workspace.trust_document.rejected:
        workspace.folders.append(FolderView(path=rejected, files=[], rejected=True))
    workspace.folders.sort(key=lambda f: f.path.lower())

    _discover_projects(workspace)
    return workspace
