"""Runtime translation: locale resolution and translator installation.

The app is authored in English; translations are optional. These tests pin the
locale-picking rules and the graceful no-catalog path, and prove that a real
``.qm`` is actually loaded and applied (not merely requested).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "minimal")

from qtpy.QtCore import QCoreApplication, QLocale  # noqa: E402
from qtpy.QtWidgets import QApplication  # noqa: E402

from maki_trust_editor import i18n  # noqa: E402

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "build_translations.py"


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _lrelease_path() -> str | None:
    """Reuse the build script's validated lrelease finder (PATH shims may be dead)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_translations", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._find_lrelease()


def test_resolve_locale_falls_back_to_system_on_garbage(app) -> None:
    system = QLocale.system().name()
    assert i18n.resolve_locale("not-a-locale").name() == system
    assert i18n.resolve_locale("").name() == system


def test_resolve_locale_honours_a_real_locale(app) -> None:
    assert i18n.resolve_locale("de_DE").language() == QLocale.Language.German


def test_available_locales_lists_compiled_catalogs(app, tmp_path) -> None:
    (tmp_path / "de.qm").write_bytes(b"")
    (tmp_path / "fr.qm").write_bytes(b"")
    (tmp_path / "app.ts").write_text("", encoding="utf-8")
    assert i18n.available_locales(tmp_path) == ["de", "fr"]


def test_install_without_catalog_does_not_change_our_strings(app, monkeypatch, tmp_path) -> None:
    """With no app .qm, our tr() strings stay English (Qt's own may still load)."""
    monkeypatch.setattr(i18n, "translations_dir", lambda: tmp_path)
    installed = i18n.install(app, requested="de_DE")
    try:
        assert QCoreApplication.translate("MainWindow", "Save") == "Save"
    finally:
        for translator in installed:
            app.removeTranslator(translator)


def test_installed_translator_actually_translates(app, tmp_path, monkeypatch) -> None:
    """A real catalog changes a looked-up string; a missing one does not.

    Uses ``de_DE`` (a locale Qt accepts) so ``resolve_locale`` keeps it; the
    catalog filename must match ``app_<locale>.qm``.
    """
    catalog = tmp_path / "app_de.qm"
    if not _compile_minimal_qm(catalog):
        pytest.skip("lrelease unavailable; cannot build a test .qm")

    monkeypatch.setattr(i18n, "translations_dir", lambda: tmp_path)
    installed = i18n.install(app, requested="de_DE")
    try:
        assert installed, "translator was not installed"
        assert QCoreApplication.translate("MainWindow", "Save") == "TRANSLATED-SAVE"
    finally:
        for translator in installed:
            app.removeTranslator(translator)
        assert QCoreApplication.translate("MainWindow", "Save") == "Save"


def _compile_minimal_qm(path) -> bool:
    """Build a one-message .qm with Qt's lrelease, or return False if absent."""
    import subprocess
    import tempfile

    lrelease = _lrelease_path()
    if lrelease is None:
        return False
    ts = path.with_suffix(".ts")
    ts.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<TS version="2.1"><context><name>MainWindow</name><message>'
        "<source>Save</source><translation>TRANSLATED-SAVE</translation>"
        "</message></context></TS>\n",
        encoding="utf-8",
    )
    with tempfile.TemporaryDirectory() as work:
        result = subprocess.run(
            [lrelease, str(ts), "-qm", str(path)],
            capture_output=True,
            text=True,
            cwd=work,
            timeout=30,
        )
    return result.returncode == 0 and path.is_file()
