"""Read and write maki's ``permissions.toml`` files.

The file has three shapes, all of which maki round-trips:

    default = "allow"            # or "deny" / "prompt"

    [bash]
    allow = ["cargo *"]          # list of scopes, or ``true`` for all scopes
    deny = ["rm -rf *"]
    default = "prompt"

    [mcp.github]
    allow = ["admin_delete", "*"]
    deny = ["admin_drop_database"]
    default = "allow"

We keep the parsed form close to the file so an edit writes back only what the
user changed, and we never invent sections maki would reject.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit

DEFAULT_KEY = "default"
ALLOW_KEY = "allow"
DENY_KEY = "deny"
MCP_KEY = "mcp"
LEGACY_ALLOW_ALL = "allow_all"

EFFECTS = ("allow", "deny", "prompt")
EFFECT_ALLOW = "allow"
EFFECT_DENY = "deny"


@dataclass
class ToolRules:
    """Rules for one native tool section, e.g. ``[bash]``."""

    name: str
    allow_all: bool = False
    deny_all: bool = False
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    default: str | None = None
    # Keys inside the section we do not model, kept verbatim so a save never
    # drops a key a newer maki wrote.
    unknown: dict[str, Any] = field(default_factory=dict)


@dataclass
class McpServerRules:
    """Rules for one ``[mcp.<server>]`` section."""

    server: str
    allow_all: bool = False
    deny_all: bool = False
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    default: str | None = None
    unknown: dict[str, Any] = field(default_factory=dict)


@dataclass
class PermissionsFile:
    """A parsed ``permissions.toml``, plus its source path for writing back."""

    path: Path
    default: str | None = None
    tools: dict[str, ToolRules] = field(default_factory=dict)
    mcp: dict[str, McpServerRules] = field(default_factory=dict)
    # Keys we do not model, kept verbatim so a save never drops them.
    _unknown: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    @classmethod
    def load(cls, path: Path) -> PermissionsFile:
        if not path.is_file():
            return cls(path=path)
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        return cls.from_document(path, doc)

    @classmethod
    def from_document(cls, path: Path, doc: Any) -> PermissionsFile:
        parsed = cls(path=path)
        for key, value in doc.items():
            if key == DEFAULT_KEY:
                parsed.default = _as_str(value)
            elif key == LEGACY_ALLOW_ALL:
                if value is True:
                    parsed.default = EFFECT_ALLOW
            elif key == MCP_KEY and isinstance(value, dict):
                for server, table in value.items():
                    if isinstance(table, dict):
                        parsed.mcp[str(server)] = _parse_mcp(str(server), table)
            elif isinstance(value, dict):
                parsed.tools[str(key)] = _parse_tool(str(key), value)
            else:
                parsed._unknown[str(key)] = value
        return parsed

    def to_toml(self) -> str:
        # A canonical order keeps the serialization byte-stable, which is what
        # lets the caller diff against the load baseline and skip unchanged
        # files. The original file's key order is not preserved.
        doc = tomlkit.document()
        if self.default is not None:
            doc[DEFAULT_KEY] = self.default
        for key, value in self._unknown.items():
            doc[key] = value
        for name in sorted(self.tools):
            doc[name] = _tool_to_table(self.tools[name])
        if self.mcp:
            mcp_table = tomlkit.table()
            for server in sorted(self.mcp):
                mcp_table[server] = _scoped_to_table(self.mcp[server])
            doc[MCP_KEY] = mcp_table
        return tomlkit.dumps(doc)


def _parse_tool(name: str, table: dict) -> ToolRules:
    rules = ToolRules(name=name)
    _apply_scoped(table, rules)
    return rules


def _parse_mcp(server: str, table: dict) -> McpServerRules:
    rules = McpServerRules(server=server)
    _apply_scoped(table, rules)
    return rules


def _apply_scoped(table: dict, rules: Any) -> None:
    for key, value in table.items():
        if key == DEFAULT_KEY:
            rules.default = _as_str(value)
        elif key == ALLOW_KEY:
            rules.allow_all, rules.allow = _split_scope_set(value)
        elif key == DENY_KEY:
            rules.deny_all, rules.deny = _split_scope_set(value)
        else:
            rules.unknown[key] = value


def _split_scope_set(value: Any) -> tuple[bool, list[str]]:
    if value is True:
        return True, []
    if value is False:
        return False, []
    if isinstance(value, (list, tuple)):
        return False, [str(item) for item in value]
    if isinstance(value, str):
        return (value == "*"), ([] if value == "*" else [value])
    return False, []


def _as_str(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _scoped_to_table(rules: Any) -> Any:
    table = tomlkit.table()
    if rules.default is not None:
        table[DEFAULT_KEY] = rules.default
    if rules.allow_all:
        table[ALLOW_KEY] = True
    elif rules.allow:
        table[ALLOW_KEY] = list(rules.allow)
    if rules.deny_all:
        table[DENY_KEY] = True
    elif rules.deny:
        table[DENY_KEY] = list(rules.deny)
    for key, value in rules.unknown.items():
        table[key] = value
    return table


def _tool_to_table(rules: ToolRules) -> Any:
    return _scoped_to_table(rules)
