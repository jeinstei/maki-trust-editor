#!/usr/bin/env python3
"""Run the maki trust editor from source, with no install step.

This is the portable alternative to ``run_app.sh``: it works with whatever
Python interpreter and Qt binding are already present, and it puts ``src`` on
the import path so the package is found without being installed. If the import
fails because a dependency is missing, it prints the exact command to fix that.

Examples::

    ./run_app.py
    python3 run_app.py
    uv run --with pyside6 run_app.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"

MISSING_HINT = (
    "Missing dependency: {name}.\n"
    "Install the app's dependencies with one of:\n"
    "    uv sync --extra pyside6      # or --extra pyqt6\n"
    "    pip install qtpy tomlkit psutil PySide6\n"
    "or run straight from source without installing anything:\n"
    "    uv run --with pyside6 --with qtpy --with tomlkit --with psutil run_app.py"
)


def _ensure_on_path() -> None:
    # The package is "not installed" by design; make it importable anyway.
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))


def _check_importable(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def main() -> int:
    _ensure_on_path()

    for module, label in (("qtpy", "qtpy"), ("tomlkit", "tomlkit"), ("psutil", "psutil")):
        if not _check_importable(module):
            print(MISSING_HINT.format(name=label), file=sys.stderr)
            return 1

    # qtpy needs at least one real Qt binding underneath it.
    if not any(_check_importable(b) for b in ("PySide6", "PySide2", "PyQt6", "PyQt5")):
        print(MISSING_HINT.format(name="a Qt binding (PySide6 or PyQt6)"), file=sys.stderr)
        return 1

    from maki_trust_editor.app import main as run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
