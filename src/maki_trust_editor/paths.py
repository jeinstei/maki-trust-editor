"""Locate the on-disk stores maki keeps, mirroring maki-storage's rules.

We reproduce the logic of ``etcetera::choose_base_strategy`` closely enough to
find the same files on every platform maki runs on, without depending on maki:

    * legacy fallback: if ``~/.maki`` exists, everything (config, data, state,
      logs) collapses to it. Checked first, exactly like maki.
    * Windows: config/state/data under ``%APPDATA%`` / ``%LOCALAPPDATA%``.
    * macOS: ``~/Library/Application Support/maki``.
    * Linux/BSD: XDG, i.e. ``$XDG_CONFIG_HOME``, ``$XDG_STATE_HOME``,
      ``$XDG_DATA_HOME`` with their ``~/.config``, ``~/.local/state``,
      ``~/.local/share`` defaults.

Nothing here mutates the filesystem.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "maki"
LEGACY_DIR = f".{APP_NAME}"
SESSIONS_DIR = "sessions"

TRUSTED_FOLDERS_FILE = "trusted-folders.json"
PRE_TRUST_FILE = "pre-trust-roots.json"

PERMISSIONS_FILE = "permissions.toml"
PROJECT_DIR = ".maki"
GIT_MARKER = ".git"

# Our own state dir for things that are not maki's (the log), so we never write
# into maki's directories.
APP_STATE_NAME = "maki-trust-editor"
LOG_FILE = "maki-trust-editor.log"

_IS_WINDOWS = sys.platform.startswith("win")
_IS_MACOS = sys.platform == "darwin"


def home() -> Path:
    # USERPROFILE on Windows, HOME elsewhere; Path.home() as the final fallback.
    override = os.environ.get("USERPROFILE") if _IS_WINDOWS else os.environ.get("HOME")
    if override:
        return Path(override)
    return Path.home()


def legacy_dir() -> Path:
    return home() / LEGACY_DIR


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def _platform_config_dir() -> Path:
    if _IS_WINDOWS:
        base = _env_path("APPDATA") or (home() / "AppData" / "Roaming")
        return base / APP_NAME
    if _IS_MACOS:
        return home() / "Library" / "Application Support" / APP_NAME
    base = _env_path("XDG_CONFIG_HOME") or (home() / ".config")
    return base / APP_NAME


def _state_root() -> Path:
    """The per-user state base (maki's is this plus ``/maki``)."""
    if _IS_WINDOWS:
        # maki keeps state on Windows under the data dir (%LOCALAPPDATA%\maki).
        return _env_path("LOCALAPPDATA") or (home() / "AppData" / "Local")
    if _IS_MACOS:
        return home() / "Library" / "Application Support"
    return _env_path("XDG_STATE_HOME") or (home() / ".local" / "state")


def config_dir() -> Path:
    """Where global config (permissions.toml, providers.toml, ...) lives."""
    legacy = legacy_dir()
    if legacy.is_dir():
        return legacy
    return _platform_config_dir()


def state_dir() -> Path:
    """Where sessions, plans, projects and the trust document live."""
    legacy = legacy_dir()
    if legacy.is_dir():
        return legacy
    return _state_root() / APP_NAME


def sessions_dir() -> Path:
    return state_dir() / SESSIONS_DIR


def trusted_folders_path() -> Path:
    return state_dir() / TRUSTED_FOLDERS_FILE


def pre_trust_roots_path() -> Path:
    """The frozen pre-trust snapshot lives beside the session index."""
    return sessions_dir() / PRE_TRUST_FILE


def global_permissions_path() -> Path:
    return config_dir() / PERMISSIONS_FILE


def project_permissions_path(project_root: Path) -> Path:
    return project_root / PROJECT_DIR / PERMISSIONS_FILE


def app_state_dir() -> Path:
    """Our own state dir (holds the log), beside maki's rather than inside it."""
    return _state_root() / APP_STATE_NAME


def log_path() -> Path:
    return app_state_dir() / LOG_FILE


def project_root_of(cwd: str | Path) -> Path:
    """The nearest ancestor holding a ``.git`` marker, else ``cwd`` itself.

    maki treats any ``.git`` entry (dir or file) as the boundary, which matters
    for worktrees and submodules where ``.git`` is a file.
    """
    path = Path(cwd)
    for candidate in (path, *path.parents):
        if (candidate / GIT_MARKER).exists():
            return candidate
    return path
