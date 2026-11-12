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


def _rule_text(tool: str, scope: str | None, effect: str) -> str:
    scope_text = scope if scope is not None else "(all scopes)"
    return f"{tool}  {scope_text}  {effect}"


def _diff_rules(old: list[sessions.SessionRule], new: list[sessions.SessionRule]) -> list[Change]:
    """Rules a write adds, removes or flips.

    Counted as a multiset, matching how the dirty check compares rules: keying
    on (tool, scope) alone would collapse duplicates, so adding a second
    identical rule looked like no change at all. A key whose effect flipped and
    whose count held is reported once, as a change, not as a swap of two rules
    that a reader would have to pair up themselves.
    """
    flipped = _flipped_keys(old, new)
    old_counts = Counter((r.tool, r.scope, r.effect) for r in old)
    new_counts = Counter((r.tool, r.scope, r.effect) for r in new)
    old_scoped = Counter({key: n for key, n in old_counts.items() if key[:2] not in flipped})
    new_scoped = Counter({key: n for key, n in new_counts.items() if key[:2] not in flipped})
    changes: list[Change] = []
    for key in sorted(set(new_scoped) - set(old_scoped)):
        changes.extend(Change(ADDED, _rule_text(*key)) for _ in range(new_scoped[key]))
    for key in sorted(set(old_scoped) - set(new_scoped)):
        changes.extend(Change(REMOVED, _rule_text(*key)) for _ in range(old_scoped[key]))
    for key in sorted(set(old_scoped) & set(new_scoped)):
        delta = new_scoped[key] - old_scoped[key]
        if delta:
            marker = ADDED if delta > 0 else REMOVED
            changes.extend(Change(marker, _rule_text(*key)) for _ in range(abs(delta)))
    return changes + _flip_changes(old, new, flipped)


def _effect_at(rules: list[sessions.SessionRule]) -> dict[tuple[str, str | None], str]:
    effects: dict[tuple[str, str | None], str] = {}
    for rule in rules:
        effects.setdefault((rule.tool, rule.scope), rule.effect)
    return effects


def _flipped_keys(
    old: list[sessions.SessionRule], new: list[sessions.SessionRule]
) -> set[tuple[str, str | None]]:
    """Keys present on both sides with a different effect, ignoring counts.

    Both sides must hold the key, or it is an addition or a removal rather than
    a change of verdict.
    """
    old_effects, new_effects = _effect_at(old), _effect_at(new)
    return {
        key for key in set(old_effects) & set(new_effects) if old_effects[key] != new_effects[key]
    }


def _flip_changes(
    old: list[sessions.SessionRule],
    new: list[sessions.SessionRule],
    flipped: set[tuple[str, str | None]],
) -> list[Change]:
    """One ``~`` line per flipped key, naming the effect it had and takes."""
    old_effects, new_effects = _effect_at(old), _effect_at(new)
    return [
        Change(
            CHANGED,
            f"{_rule_text(key[0], key[1], new_effects[key])}  (was {old_effects[key]})",
        )
        for key in sorted(flipped)
    ]


def _scope_pairs(allow: list[str], deny: list[str]) -> Counter[tuple[str, str]]:
    return Counter(
        [(scope, permissions.EFFECT_ALLOW) for scope in allow]
        + [(scope, permissions.EFFECT_DENY) for scope in deny]
    )


def _section_changes(
    name: str, old: permissions.ToolRules | None, new: permissions.ToolRules | None
) -> list[Change]:
    """Rules and settings a write changes in one section.

    Scopes are not the whole story: a section can appear or vanish on its own,
    carry a ``default`` that decides every scope it does not list, and keep
    unknown keys verbatim. Diffing scopes alone reported none of those, so a
    section added with a default, or removed wholesale, was written without the
    dialog naming it — and a save whose only change was such a section read as
    "nothing to save".
    """
    changes: list[Change] = []
    if old is None:
        # A section arriving with a default says so on its own line, or the
        # reviewer cannot tell a bare header from one that sets a fallback.
        suffix = "" if new.default is None else f"  default {new.default}"
        changes.append(Change(ADDED, f"[{name}]{suffix}"))
    elif new is None:
        changes.append(Change(REMOVED, f"[{name}]"))
    elif old.default != new.default:
        changes.append(Change(CHANGED, f"[{name}] default  {new.default}  (was {old.default})"))
    old_unknown = old.unknown if old is not None else {}
    new_unknown = new.unknown if new is not None else {}
    for key in sorted(set(new_unknown) | set(old_unknown)):
        if key not in new_unknown:
            changes.append(Change(REMOVED, f"[{name}] {key}"))
        elif key not in old_unknown:
            changes.append(Change(ADDED, f"[{name}] {key}"))
        elif old_unknown[key] != new_unknown[key]:
            changes.append(Change(CHANGED, f"[{name}] {key}  {new_unknown[key]}"))
    return changes + _scope_changes(name, old, new)


def _scope_changes(
    name: str, old: permissions.ToolRules | None, new: permissions.ToolRules | None
) -> list[Change]:
    old_pairs = _scope_pairs(old.allow, old.deny) if old is not None else Counter()
    new_pairs = _scope_pairs(new.allow, new.deny) if new is not None else Counter()
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
        changes.extend(_section_changes(name, old.tools.get(name), new.tools.get(name)))
    for server in sorted(set(new.mcp) | set(old.mcp)):
        changes.extend(_section_changes(f"mcp.{server}", old.mcp.get(server), new.mcp.get(server)))
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
