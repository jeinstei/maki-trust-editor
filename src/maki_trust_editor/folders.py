"""Read and write the folder-trust document and frozen pre-trust roots.

``trusted-folders.json`` is a single global document under the state dir:

    {
      "version": 2,
      "folders": [{"path": "/home/me/proj", "files": ["permissions"]}],
      "rejected": ["/home/me/other"]
    }

Removing a folder is a ``folders`` entry change; rejecting is a ``rejected``
entry. We add, update and remove those two lists and keep ``version`` and any
key we do not model intact.

``pre-trust-roots.json`` (see :class:`PreTrustRoots`) is a separate, flat JSON
array of project roots that predate folder trust, kept beside the session index.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DOCUMENT_VERSION = 2


@dataclass
class FolderEntry:
    path: str
    files: list[str] = field(default_factory=list)
    # Keys inside the entry we do not model, kept verbatim so a save never drops
    # a field a newer maki wrote.
    unknown: dict = field(default_factory=dict)


@dataclass
class TrustDocument:
    path: Path
    version: int = DOCUMENT_VERSION
    folders: list[FolderEntry] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    # Anything else in the JSON is preserved verbatim on save.
    _unknown: dict = field(default_factory=dict, repr=False)

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    @classmethod
    def load(cls, path: Path) -> TrustDocument:
        if not path.is_file():
            return cls(path=path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        doc = cls(path=path, version=int(raw.get("version", DOCUMENT_VERSION)))
        for entry in raw.get("folders", []):
            doc.folders.append(
                FolderEntry(
                    path=str(entry.get("path", "")),
                    files=[str(f) for f in entry.get("files", [])],
                    unknown={k: v for k, v in entry.items() if k not in ("path", "files")},
                )
            )
        doc.rejected = [str(p) for p in raw.get("rejected", [])]
        for key, value in raw.items():
            if key not in ("version", "folders", "rejected"):
                doc._unknown[key] = value
        return doc

    def to_json(self) -> str:
        payload: dict = {"version": self.version}
        payload.update(self._unknown)
        payload["folders"] = [
            {"path": e.path, "files": list(e.files), **e.unknown} for e in self.folders
        ]
        payload["rejected"] = list(self.rejected)
        return json.dumps(payload, indent=2) + "\n"

    def add_or_update_folder(self, path: str, files: list[str]) -> bool:
        for entry in self.folders:
            if entry.path == path:
                entry.files = list(files)
                return True
        self.folders.append(FolderEntry(path=path, files=list(files)))
        return True

    def remove_folder(self, path: str) -> bool:
        before = len(self.folders)
        self.folders = [e for e in self.folders if e.path != path]
        return len(self.folders) != before

    def add_rejected(self, path: str) -> bool:
        if path in self.rejected:
            return False
        self.rejected.append(path)
        return True

    def remove_rejected(self, path: str) -> bool:
        before = len(self.rejected)
        self.rejected = [p for p in self.rejected if p != path]
        return len(self.rejected) != before


@dataclass
class PreTrustRoots:
    """The frozen snapshot of roots that predate folder trust.

    A flat JSON array of path strings; maki sorts and dedups it on write, so we
    do the same to keep the file stable. A missing file stays a legitimately
    absent store (Save creates it only after a real edit).
    """

    path: Path
    roots: list[str] = field(default_factory=list)

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    @classmethod
    def load(cls, path: Path) -> PreTrustRoots:
        if not path.is_file():
            return cls(path=path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(path=path, roots=[str(p) for p in raw])

    def to_json(self) -> str:
        return json.dumps(sorted(set(self.roots)), indent=2) + "\n"

    def add(self, root: str) -> bool:
        if root in self.roots:
            return False
        self.roots.append(root)
        return True

    def remove(self, root: str) -> bool:
        before = len(self.roots)
        self.roots = [p for p in self.roots if p != root]
        return len(self.roots) != before
