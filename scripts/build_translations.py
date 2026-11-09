#!/usr/bin/env python3
"""Compile ``.ts`` translation sources to ``.qm`` binaries.

The app loads ``.qm`` files from ``src/maki_trust_editor/translations``; a ``.ts``
is the human-edited source that Qt Linguist works on. Run this after editing a
``.ts`` (or use ``--extract`` to refresh the ``.ts`` from the source strings).

``lrelease`` comes from Qt, not from ``pip``: it ships with the Qt SDK, with
some distro Qt packages, and (for PyQt6) alongside the binding. This script
looks for a working one and says so plainly when there is none, because a
missing compiler is not an error while the app is English-only.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

TRANSLATIONS_DIR = Path(__file__).resolve().parent.parent / "src/maki_trust_editor/translations"
SOURCE_GLOB = "*.py"
LRELEASE_NAMES = ("lrelease", "lrelease6", "lrelease-qt6")
PYLUPDATE_NAMES = ("pylupdate6", "pylupdate")
# ``app.ts`` is the English template translators copy; only ``app_<locale>.ts``
# files are compiled into catalogs.
TEMPLATE_STEM = "app"
# On Debian/Ubuntu the PATH entry can be a ``qtchooser`` shim that fails when no
# Qt version is configured, while the real binary sits in a versioned Qt bin dir.
QT_BIN_DIRS = (
    "/usr/lib/qt6/bin",
    "/usr/lib/x86_64-linux-gnu/qt6/bin",
    "/usr/lib/qt5/bin",
    "/usr/local/opt/qt6/bin",
    "/opt/homebrew/opt/qt6/bin",
)


def _works(path: str) -> bool:
    """True when the executable actually runs (a shim that cannot exec returns False).

    ``-version`` is the form ``lrelease`` and ``lupdate`` accept; ``pylupdate6``
    accepts only ``-V`` / ``--version`` and exits non-zero on ``-version``, so try
    both before giving up.
    """
    for flag in ("-version", "-V"):
        try:
            out = subprocess.run([path, flag], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0:
            return True
    return False


def _find_tool(names: tuple[str, ...]) -> str | None:
    """A working tool: PATH first, then the versioned Qt bin dirs.

    Every candidate is validated by running it, so a broken ``qtchooser`` shim on
    PATH is skipped in favour of the real binary under ``/usr/lib/qt6/bin``.
    """
    for name in names:
        path = shutil.which(name)
        if path and _works(path):
            return path
    for directory in QT_BIN_DIRS:
        for name in names:
            candidate = Path(directory) / name
            if candidate.is_file() and _works(str(candidate)):
                return str(candidate)
    return None


def _find_lrelease() -> str | None:
    return _find_tool(LRELEASE_NAMES)


def _find_pylupdate() -> str | None:
    return _find_tool(PYLUPDATE_NAMES) or _find_tool(("lupdate",))


def extract() -> int:
    """Refresh ``app.ts`` from the ``tr()`` calls in the source."""
    tool = _find_pylupdate()
    if tool is None:
        print("pylupdate6 not found; install PyQt6 tools to refresh the .ts", file=sys.stderr)
        return 1
    sources = sorted(str(p) for p in (TRANSLATIONS_DIR.parent).glob(SOURCE_GLOB))
    target = TRANSLATIONS_DIR / "app.ts"
    result = subprocess.run([tool, "-no-obsolete", "--ts", str(target), *sources])
    return result.returncode


def compile_ts() -> int:
    tool = _find_lrelease()
    if tool is None:
        print(
            "No working lrelease found. Translations are optional: the app runs in "
            "English until a .qm is present. Install Qt's Linguist tools to compile.",
            file=sys.stderr,
        )
        return 0

    locale_sources = sorted(p for p in TRANSLATIONS_DIR.glob("*.ts") if p.stem != TEMPLATE_STEM)
    if not locale_sources:
        print("no locale .ts files to compile", file=sys.stderr)
        return 0

    for ts in locale_sources:
        result = subprocess.run([tool, str(ts), "-qm", str(ts.with_suffix(".qm"))])
        if result.returncode != 0:
            return result.returncode
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--extract",
        action="store_true",
        help="refresh app.ts from the tr() calls before compiling",
    )
    args = parser.parse_args()
    if args.extract:
        code = extract()
        if code != 0:
            return code
    return compile_ts()


if __name__ == "__main__":
    raise SystemExit(main())
