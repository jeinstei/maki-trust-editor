"""Safe writing: backup, atomic replace, and a per-target maki-closed guard.

Editing these files while maki runs races its own writers, but the risk is not
uniform:

* a **session file** is only appended by the one maki process that has it open,
  so the real hazard is ``running_processes > 0`` only insofar as we cannot
  map a pid to a session. We therefore treat a session write as guarded when
  *any* maki runs, and offer a per-target override.
* **folder trust** and **permissions.toml** files are shared state that any
  maki process may write, so they stay guarded whenever any maki runs.

The guard never signals maki; it only detects and reports. Detection is
cross-platform: psutil when installed, otherwise native commands per OS.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

BACKUP_SUFFIX = ".bak"
PROCESS_NAMES = ("maki", "maki.exe")

MAKI_RUNNING_MESSAGE = (
    "maki is running (pid {pids}). Writes to shared files are blocked while it "
    "runs; session writes are allowed but may be overwritten when that session "
    "exits."
)
MAKI_NOT_RUNNING_MESSAGE = "No maki process found."

_IS_WINDOWS = sys.platform.startswith("win")
_IS_LINUX = sys.platform.startswith("linux")


@dataclass
class MakiStatus:
    running: bool
    pids: list[int]

    @property
    def message(self) -> str:
        if not self.running:
            return MAKI_NOT_RUNNING_MESSAGE
        return MAKI_RUNNING_MESSAGE.format(pids=", ".join(str(p) for p in self.pids))


def _stems() -> set[str]:
    return {name[:-4] if name.endswith(".exe") else name for name in PROCESS_NAMES}


def _pids_via_psutil() -> list[int] | None:
    # psutil is an optional extra: present, it gives one cross-platform path;
    # absent, detect_maki falls back to native commands. Return None means
    # "not installed", not "no match".
    try:
        import psutil  # type: ignore
    except ImportError:
        return None
    stems = _stems()
    pids: list[int] = []
    for proc in psutil.process_iter(["pid", "name"]):
        name = (proc.info.get("name") or "").lower()
        stem = name[:-4] if name.endswith(".exe") else name
        if stem in stems:
            pids.append(int(proc.info["pid"]))
    return pids


def _pids_via_tasklist() -> list[int]:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq maki.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in out.stdout.splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 2 and parts[0].lower() in ("maki.exe", "maki"):
            if parts[1].isdigit():
                pids.append(int(parts[1]))
    return pids


def _pids_via_pgrep() -> list[int]:
    pids: list[int] = []
    for name in _stems():
        try:
            out = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True, timeout=5)
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
        if out.returncode == 0:
            pids.extend(int(t) for t in out.stdout.split() if t.isdigit())
    return pids


def detect_maki() -> MakiStatus:
    """Best-effort detection of a running maki process, never signalling it."""
    pids = _pids_via_psutil()
    if pids is None:
        pids = _pids_via_tasklist() if _IS_WINDOWS else _pids_via_pgrep()
    pids = sorted(set(pids))
    return MakiStatus(running=bool(pids), pids=pids)


def file_in_use(path: Path, pids: list[int]) -> bool:
    """True when a live maki pid holds ``path`` open (Linux /proc only).

    On other platforms we cannot see open handles without extra privileges, so
    we return False and let the caller fall back to the process-level guard.
    """
    if not _IS_LINUX or not path.exists():
        return False
    try:
        target = os.path.realpath(path)
    except OSError:
        return False
    for pid in pids:
        fd_dir = Path(f"/proc/{pid}/fd")
        try:
            entries = list(fd_dir.iterdir())
        except (PermissionError, FileNotFoundError, OSError):
            continue
        for fd in entries:
            try:
                if os.path.realpath(fd) == target:
                    return True
            except OSError:
                continue
    return False


@dataclass
class WriteTarget:
    """One file to write, with how strict its guard must be.

    ``shared`` is True for permissions.toml and the trust document: any maki
    may write them, so a running maki blocks the write. ``shared`` is False for
    a session file: only the process holding it is a hazard, so a running maki
    triggers a warning rather than a hard block.

    ``content`` is written verbatim by default. A session passes ``writer``
    instead: its file is rewritten in place (prefix streamed, only the last line
    replaced), so ``content`` is left unused for it.
    """

    path: Path
    content: str = ""
    shared: bool = True
    writer: Callable[[], None] | None = field(default=None, compare=False)


def _real_target(path: Path) -> Path:
    """The file a write must land on, following a symlink to its target.

    ``os.replace`` would otherwise replace the symlink itself with a regular
    file, silently breaking a dotfile-managed ``permissions.toml`` link. A path
    that does not exist yet is returned unchanged (nothing to resolve).
    """
    if path.is_symlink():
        return path.resolve()
    return path


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + BACKUP_SUFFIX)


def make_backup(path: Path) -> Path | None:
    real = _real_target(path)
    if not real.is_file():
        return None
    target = backup_path(real)
    shutil.copy2(real, target)
    return target


def atomic_write_text(path: Path, content: str) -> None:
    real = _real_target(path)
    real.parent.mkdir(parents=True, exist_ok=True)
    tmp = real.with_name(real.name + ".tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    os.replace(tmp, real)


@dataclass
class SaveResult:
    backups: list[Path]

    def describe(self) -> str:
        if not self.backups:
            return "Saved (no previous file to back up)."
        return "Saved. Backups: " + ", ".join(str(p) for p in self.backups)


def blocked_targets(targets: list[WriteTarget], status: MakiStatus) -> list[WriteTarget]:
    """Shared targets that must not be written while maki runs."""
    if not status.running:
        return []
    if _IS_LINUX:
        return [t for t in targets if t.shared and file_in_use(t.path, status.pids)]
    # On non-Linux platforms, block ALL shared files since we can't detect file-in-use
    return [t for t in targets if t.shared]


def write_targets(targets: list[WriteTarget]) -> SaveResult:
    """Back up and write each target; the caller decides which (usually one)."""
    backups: list[Path] = []
    for target in targets:
        backup = make_backup(target.path)
        if backup is not None:
            backups.append(backup)
        if target.writer is not None:
            target.writer()
        else:
            atomic_write_text(target.path, target.content)
    return SaveResult(backups=backups)
