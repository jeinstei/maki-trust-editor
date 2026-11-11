"""What a save is about to change, computed from the file on disk.

The save dialog answers "what will this write do". The in-memory baseline is
what the app *loaded*, which a running maki can have moved since; diffing
against it would describe a write that is not the one about to happen. This
reads the target file, or treats it as empty when it does not exist yet, and
reports the rules that appear, disappear or change effect.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import folders, permissions, sessions

ADDED = "+"
REMOVED = "-"
CHANGED = "~"


@dataclass
class Change:
    """One rule that a save adds, removes or rewrites, ready to display."""

    marker: str
    text: str

    def line(self) -> str:
        return f"{self.marker} {self.text}"


@dataclass
class ChangeList:
    """Every change for one file, plus whether the file moved under us."""

    path: Path
    changes: list[Change] = field(default_factory=list)
    stale: bool = False

    @property
    def empty(self) -> bool:
        return not self.changes


def _rule_key(rule: sessions.SessionRule) -> tuple[str, str | None, str]:
    return (rule.tool, rule.scope, rule.effect)


def _rule_text(tool: str, scope: str | None, effect: str) -> str:
    scope_text = scope if scope is not None else "(all scopes)"
    return f"{tool}  {scope_text}  {effect}"


def _diff_rules(old: list[sessions.SessionRule], new: list[sessions.SessionRule]) -> list[Change]:
    """Rules that differ, keyed by (tool, scope) so an effect flip reads as one change.

    Effect is excluded from the key on purpose: flipping allow to deny is one
    changed rule, not a removal plus an addition.
    """
    old_by_key = {(r.tool, r.scope): r.effect for r in old}
    new_by_key = {(r.tool, r.scope): r.effect for r in new}
    changes: list[Change] = []
    for key in sorted(set(new_by_key) - set(old_by_key)):
        changes.append(Change(ADDED, _rule_text(key[0], key[1], new_by_key[key])))
    for key in sorted(set(old_by_key) - set(new_by_key)):
        changes.append(Change(REMOVED, _rule_text(key[0], key[1], old_by_key[key])))
    for key in sorted(set(old_by_key) & set(new_by_key)):
        if old_by_key[key] != new_by_key[key]:
            changes.append(
                Change(
                    CHANGED,
                    f"{_rule_text(key[0], key[1], new_by_key[key])}" f"  (was {old_by_key[key]})",
                )
            )
    return changes


def _scope_pairs(allow: list[str], deny: list[str]) -> Counter[tuple[str, str]]:
    return Counter(
        [(scope, permissions.EFFECT_ALLOW) for scope in allow]
        + [(scope, permissions.EFFECT_DENY) for scope in deny]
    )


def _section_changes(
    name: str, old: permissions.ToolRules | None, new: permissions.ToolRules
) -> list[Change]:
    old_pairs = _scope_pairs(old.allow, old.deny) if old is not None else Counter()
    new_pairs = _scope_pairs(new.allow, new.deny)
    old_by_scope = {scope: effect for scope, effect in old_pairs}
    new_by_scope = {scope: effect for scope, effect in new_pairs}
    changes: list[Change] = []
    for scope in sorted(set(new_by_scope) - set(old_by_scope)):
        changes.append(Change(ADDED, _rule_text(name, scope, new_by_scope[scope])))
    for scope in sorted(set(old_by_scope) - set(new_by_scope)):
        changes.append(Change(REMOVED, _rule_text(name, scope, old_by_scope[scope])))
    for scope in sorted(set(old_by_scope) & set(new_by_scope)):
        if old_by_scope[scope] != new_by_scope[scope]:
            changes.append(
                Change(
                    CHANGED,
                    f"{_rule_text(name, scope, new_by_scope[scope])}"
                    f"  (was {old_by_scope[scope]})",
                )
            )
    return changes


def _permissions_changes(
    path: Path, old: permissions.PermissionsFile, new: permissions.PermissionsFile
) -> ChangeList:
    changes: list[Change] = []
    if old.default != new.default:
        changes.append(Change(CHANGED, f"(file default)  {new.default}  (was {old.default})"))
    for name in sorted(set(new.tools) | set(old.tools)):
        if name in new.tools:
            changes.extend(_section_changes(name, old.tools.get(name), new.tools[name]))
    for server in sorted(set(new.mcp) | set(old.mcp)):
        if server in new.mcp:
            changes.extend(_section_changes(f"mcp.{server}", old.mcp.get(server), new.mcp[server]))
    return ChangeList(path=path, changes=changes)


def session_changes(path: Path, new_rules: list[sessions.SessionRule]) -> ChangeList:
    """Rules a session save writes, against what the file holds now."""
    try:
        old_rules = sessions.Session.load(path).rules
    except (OSError, ValueError):
        old_rules = []
    return ChangeList(path=path, changes=_diff_rules(old_rules, new_rules))


def permissions_changes(path: Path, new: permissions.PermissionsFile) -> ChangeList:
    """Rules a permissions save writes, against the file on disk now."""
    try:
        old = permissions.PermissionsFile.load(path)
    except (OSError, ValueError):
        return ChangeList(path=path, changes=_diff_permissions_added(new))
    return _permissions_changes(path, old, new)


def _diff_permissions_added(new: permissions.PermissionsFile) -> list[Change]:
    empty = permissions.PermissionsFile(path=new.path)
    return _permissions_changes(new.path, empty, new).changes


def folder_changes(
    path: Path,
    new_doc: folders.TrustDocument,
    new_roots: folders.PreTrustRoots | None = None,
) -> ChangeList:
    """Folder trust entries that a save adds or removes."""
    try:
        old = folders.TrustDocument.load(path)
    except (OSError, ValueError):
        old = folders.TrustDocument(path=path)
    changes: list[Change] = []
    old_folders = {entry.path: entry.files for entry in old.folders}
    new_folders = {entry.path: entry.files for entry in new_doc.folders}
    for folder in sorted(set(new_folders) - set(old_folders)):
        changes.append(Change(ADDED, f"trusted  {folder}"))
    for folder in sorted(set(old_folders) - set(new_folders)):
        changes.append(Change(REMOVED, f"trusted  {folder}"))
    for folder in sorted(set(old_folders) & set(new_folders)):
        if old_folders[folder] != new_folders[folder]:
            changes.append(Change(CHANGED, f"trusted  {folder}  (gated files changed)"))
    for rejected in sorted(set(new_doc.rejected) - set(old.rejected)):
        changes.append(Change(ADDED, f"rejected  {rejected}"))
    for rejected in sorted(set(old.rejected) - set(new_doc.rejected)):
        changes.append(Change(REMOVED, f"rejected  {rejected}"))
    if new_roots is not None:
        try:
            old_roots = folders.PreTrustRoots.load(new_roots.path).roots
        except (OSError, ValueError):
            old_roots = []
        for root in sorted(set(new_roots.roots) - set(old_roots)):
            changes.append(Change(ADDED, f"pre-trust root  {root}"))
        for root in sorted(set(old_roots) - set(new_roots.roots)):
            changes.append(Change(REMOVED, f"pre-trust root  {root}"))
    return ChangeList(path=path, changes=changes)
