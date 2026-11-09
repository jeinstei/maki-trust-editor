"""Read and rewrite per-session permission rules.

A session file is newline-delimited JSON. The header line has ``t: "header"``,
messages have ``t: "msg"``, and the *last* line carries the live session state,
including the ``session_rules`` list. Older meta lines are historical snapshots;
only the last line is authoritative, which matches how maki itself reads a
session back (it seeks to the end and parses the final line only).

Each rule is::

    {"tool": "bash", "scope": "git *", "effect": "allow"}

``scope`` is optional: a rule without one applies to every scope for its tool.
We rewrite ``session_rules`` inside the last line and leave every byte before it
untouched, so an 80 MB transcript is never re-encoded to change a handful of
rules. Files are newline-delimited with a bare ``\\n`` (maki writes with Rust's
``File``, which never translates line endings), so we read and split bytes and
never depend on the platform's text-mode newline handling.

Session rule effects are only ``allow`` or ``deny``; ``prompt`` belongs to
``permissions.toml``.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

META_TAG = "meta"
HEADER_TAG = "header"
RULES_KEY = "session_rules"
TOOL_KEY = "tool"
SCOPE_KEY = "scope"
EFFECT_KEY = "effect"

EFFECT_ALLOW = "allow"
EFFECT_DENY = "deny"
# Session rules only ever allow or deny; ``prompt`` is a permissions.toml
# concept. ``maki-storage``'s ``StoredEffect`` has exactly these two variants.
EFFECTS = (EFFECT_ALLOW, EFFECT_DENY)

# A session's last line holds all the state we need. Start with this many tail
# bytes and double until the final newline is found, so an unusually large meta
# record still loads without reading the whole file.
TAIL_START = 64 * 1024
NEWLINE = b"\n"
_COPY_CHUNK = 1024 * 1024

# maki's session ids are a UUID (16 bytes) displayed base58 (maki-storage's
# ``MakiId``). A UUIDv7 embeds a 48-bit Unix-millisecond timestamp; a legacy v4
# does not, which is why a decoded v4 has no creation time.
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_UUID_BYTES = 16
_MILLIS = 1000

# A running maki holds an advisory kernel lock (maki-storage's ``Lock``) on an
# empty ``sessions/locks/<session-id>`` file. The file is never deleted, so only
# an actual held lock means the session is open: taking a shared lock non-
# blocking succeeds when nobody holds the exclusive one.
LOCKS_DIR_NAME = "locks"


@dataclass
class SessionRule:
    tool: str
    scope: str | None = None
    effect: str = EFFECT_ALLOW

    def to_dict(self) -> dict:
        payload: dict = {TOOL_KEY: self.tool}
        if self.scope is not None:
            payload[SCOPE_KEY] = self.scope
        payload[EFFECT_KEY] = self.effect
        return payload


@dataclass(frozen=True)
class DecodedId:
    """A session id taken apart for display.

    ``uuid`` is the canonical hyphenated form and ``version`` the UUID version
    (7 for ids maki writes today, 4 for legacy ones). ``created`` is the
    timestamp the id itself encodes, in UTC, or ``None`` for a legacy id that
    carries none. ``None`` throughout means the id did not parse at all.
    """

    uuid: str
    version: int | None
    created: datetime.datetime | None


def _decode_base58(text: str) -> bytes | None:
    number = 0
    for char in text:
        index = _BASE58_ALPHABET.find(char)
        if index < 0:
            return None
        number = number * len(_BASE58_ALPHABET) + index
    body = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return b"\x00" * (len(text) - len(text.lstrip(_BASE58_ALPHABET[0]))) + body


def decode_session_id(session_id: str) -> DecodedId | None:
    """Take a session id apart for display, or ``None`` if it does not parse.

    Mirrors ``MakiId``: a hyphenated or bare-hex UUID first, then base58. A v7
    id yields the millisecond timestamp baked into its first 48 bits; a v4 has
    none (it is random), so ``created`` is ``None``.
    """
    raw = None
    try:
        raw = uuid.UUID(session_id).bytes
    except (ValueError, AttributeError):
        candidate = _decode_base58(session_id)
        if candidate is not None and len(candidate) == _UUID_BYTES:
            raw = candidate
    if raw is None:
        return None
    parsed = uuid.UUID(bytes=raw)
    created = None
    if parsed.version == 7:
        millis = int.from_bytes(raw[:6], "big")
        created = datetime.datetime.fromtimestamp(millis / _MILLIS, datetime.timezone.utc)
    return DecodedId(uuid=str(parsed), version=parsed.version, created=created)


@dataclass
class ToolGroup:
    """The session rules for one tool, grouped.

    A rule with no ``scope`` means "every scope for this tool"; it is kept as
    ``all_effect``. Every other rule is a ``(scope, effect)`` pair. Round-trips
    both shapes, so a scope-less rule is never lost.
    """

    tool: str
    all_effect: str | None = None
    scopes: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def group(cls, rules: list[SessionRule]) -> list[ToolGroup]:
        groups: dict[str, ToolGroup] = {}
        for rule in rules:
            group = groups.setdefault(rule.tool, cls(tool=rule.tool))
            if rule.scope is None:
                group.all_effect = rule.effect
            else:
                group.scopes.append((rule.scope, rule.effect))
        return [groups[tool] for tool in sorted(groups)]

    def to_rules(self) -> list[SessionRule]:
        rules = [
            SessionRule(tool=self.tool, scope=scope, effect=effect) for scope, effect in self.scopes
        ]
        if self.all_effect is not None:
            rules.append(SessionRule(tool=self.tool, scope=None, effect=self.all_effect))
        return rules


def canonical_rules(rules: list[SessionRule]) -> list[SessionRule]:
    """Rules in the canonical order a save writes (tools sorted, scopes first).

    A freshly loaded session keeps the file's raw order, which may differ from
    this, so it must be canonicalized before it is compared with the baseline or
    an untouched session would look edited.
    """
    return [rule for group in ToolGroup.group(rules) for rule in group.to_rules()]


@dataclass
class Session:
    path: Path
    session_id: str = ""
    cwd: str = ""
    model: str = ""
    title: str = ""
    created_at: int = 0
    updated_at: int = 0
    rules: list[SessionRule] = field(default_factory=list)
    # Where the final line begins, and that line without its newline. The bytes
    # before the offset are never loaded: a save streams them straight through.
    # ``_meta`` is the parsed final-line record when it is a meta record.
    _last_offset: int = field(default=0, repr=False)
    _last_line: bytes = field(default=b"", repr=False)
    _meta: dict | None = field(default=None, repr=False)

    @classmethod
    def load(cls, path: Path) -> Session:
        """Read the header (first line) and the last line only.

        maki reads a session back by taking the first line as the header and the
        final line as its state, so anything in between is dead weight we never
        need. That keeps opening an 80 MB transcript as cheap as opening a small
        one. Both lines are read as bytes: the prefix is never decoded, so any
        encoding maki wrote survives a save untouched.
        """
        offset, last = _read_last_line(path)
        session = cls(path=path, _last_offset=offset, _last_line=last)
        session._apply_header(_read_first_line(path))
        record = _parse_record(last)
        if record is not None and record.get("t") == META_TAG:
            session._meta = record
            session.title = str(record.get("title", ""))
            session.updated_at = int(record.get("updated_at", 0) or 0)
            for rule in record.get(RULES_KEY, []):
                session.rules.append(
                    SessionRule(
                        tool=str(rule.get(TOOL_KEY, "")),
                        scope=str(rule[SCOPE_KEY]) if SCOPE_KEY in rule else None,
                        effect=str(rule.get(EFFECT_KEY, EFFECT_ALLOW)),
                    )
                )
        return session

    def _apply_header(self, raw: bytes) -> None:
        record = _parse_record(raw)
        if record is None or record.get("t") != HEADER_TAG:
            return
        self.session_id = str(record.get("id", ""))
        self.cwd = str(record.get("cwd", ""))
        self.model = str(record.get("model", ""))
        self.created_at = int(record.get("created_at", 0) or 0)

    @property
    def has_rules_line(self) -> bool:
        """Whether the file ends with a meta record we can rewrite in place."""
        return self._meta is not None

    def rules_line(self) -> bytes:
        """The final line with ``session_rules`` replaced, newline-terminated.

        Identity for change detection: the bytes before the final line never
        change, so two states of a session differ only in this line. When the
        final line is not a meta record the file is returned unchanged.
        """
        if self._meta is None:
            return self._last_line + NEWLINE
        record = dict(self._meta)
        record[RULES_KEY] = [rule.to_dict() for rule in self.rules]
        return json.dumps(record, ensure_ascii=False).encode("utf-8") + NEWLINE

    def write(self) -> None:
        """Atomically replace the file, streaming the prefix and rewriting the
        final line.

        The prefix is copied with ``copyfileobj`` so an 80 MB transcript is never
        held in memory, and the swap is atomic so a crash can't truncate it. A
        caller keeps a backup first (see ``safe_write``).
        """
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(self.path, "rb") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst, length=_COPY_CHUNK)
            dst.truncate(self._last_offset)
            dst.seek(self._last_offset)
            dst.write(self.rules_line())
        os.replace(tmp, self.path)


def _read_last_line(path: Path) -> tuple[int, bytes]:
    """The byte offset where the final line begins, and that line without its
    trailing newline.

    Reads only a growing tail so large files are cheap. The final line is
    whatever follows the last ``\\n``; a file with no newline is all one line
    starting at offset 0.
    """
    with open(path, "rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        if size == 0:
            return 0, b""
        tail = min(TAIL_START, size)
        while True:
            handle.seek(size - tail)
            buf = handle.read(tail)
            content = buf[:-1] if buf.endswith(NEWLINE) else buf
            at = content.rfind(NEWLINE)
            if at != -1:
                return size - tail + at + 1, content[at + 1 :]
            if tail >= size:
                return 0, content
            tail = min(tail * 2, size)


def _read_first_line(path: Path) -> bytes:
    """The first line of the file without its newline, read as one chunk."""
    with open(path, "rb") as handle:
        raw = handle.readline()
    return raw[:-1] if raw.endswith(NEWLINE) else raw


def _parse_record(raw: bytes):
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def discover_sessions(sessions_dir: Path) -> list[Path]:
    """The live session files, newest first.

    Only the top-level ``sessions/*.jsonl`` files are listed. ``sessions/
    archive/<id>/*.jsonl`` holds numbered pre-compaction chunks of the *same*
    session, and the live file is the latest word on its ``session_rules``, so
    the archive is ignored: listing it would show one session several times.
    Lock and cache files are not ``*.jsonl`` and so are already excluded.
    """
    if not sessions_dir.is_dir():
        return []
    found = list(sessions_dir.glob("*.jsonl"))
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def is_locked(sessions_dir: Path, session_id: str) -> bool:
    """Whether a running maki currently holds this session's lock.

    maki takes an exclusive kernel lock on ``sessions/locks/<id>`` (an empty
    file it never deletes), so probing with a non-blocking shared lock answers
    "is anyone using this?" without disturbing the holder. A missing file, an
    unreadable one, or a platform without advisory locks all read as unlocked:
    the worst case is a session shown as closed.
    """
    path = sessions_dir / LOCKS_DIR_NAME / session_id
    if not path.is_file():
        return False
    try:
        handle = open(path, "rb")
    except OSError:
        return False
    with handle:
        try:
            if sys.platform.startswith("win"):
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            return True
    return False
