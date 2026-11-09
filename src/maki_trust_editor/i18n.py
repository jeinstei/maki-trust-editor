"""Runtime translation: pick a locale and install Qt translators.

The source language is English: every user-facing string is written in English
and marked with ``self.tr(...)`` (or ``QCoreApplication.translate`` where there
is no ``QObject``), so ``pylupdate6`` can extract it into a ``.ts`` file. A
translated ``.ts`` is compiled to ``.qm`` and dropped into ``translations/``;
this module loads the best match for the system locale and installs it.

Qt's own strings (the standard dialog buttons, for example) come from Qt's
bundled ``qtbase_<locale>.qm``, so a second translator covers those. Both are
returned to the caller: a ``QTranslator`` is owned by C++, and Python must keep
a reference or it is garbage-collected and the translations silently vanish.
"""

from __future__ import annotations

from pathlib import Path

from qtpy.QtCore import QCoreApplication, QLibraryInfo, QLocale, QTranslator

TRANSLATIONS_DIR_NAME = "translations"
LANG_ENV = "MAKI_TRUST_EDITOR_LANG"
_QM_SUFFIX = ".qm"


def translations_dir() -> Path:
    """The directory holding our compiled ``.qm`` files."""
    return Path(__file__).resolve().parent / TRANSLATIONS_DIR_NAME


def available_locales(directory: Path | None = None) -> list[str]:
    """Locales that have a compiled catalog, e.g. ``["de", "fr"]``."""
    directory = directory or translations_dir()
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.iterdir() if p.suffix == _QM_SUFFIX)


def resolve_locale(requested: str | None = None) -> QLocale:
    """The locale to translate into.

    ``requested`` (or ``MAKI_TRUST_EDITOR_LANG``) wins when it names a real
    locale; otherwise the system locale is used. An empty or malformed override
    parses to ``QLocale.Language.C`` (Qt's "no locale"), which we treat as
    "unset" and fall back to the system locale, so a typo in the environment
    never breaks startup.
    """
    override = requested or QLocale.system().name()
    locale = QLocale(override)
    if locale.language() == QLocale.Language.C:
        return QLocale.system()
    return locale


def _load(translator: QTranslator, directory: Path, prefix: str, locale: QLocale) -> bool:
    """Try ``<prefix>_<locale>`` then the language-only form; True if loaded."""
    for candidate in (locale.name(), locale.name().split("_")[0]):
        if translator.load(str(directory / f"{prefix}_{candidate}{_QM_SUFFIX}")):
            return True
    return False


def install(app: QCoreApplication, requested: str | None = None) -> list[QTranslator]:
    """Install our catalog and Qt's, if present, and return them (keep alive).

    Returns an empty list when neither is available — the app then runs in
    English, which is always a valid state since the source strings are English.
    """
    if requested is None:
        from os import environ

        requested = environ.get(LANG_ENV)

    locale = resolve_locale(requested)
    installed: list[QTranslator] = []

    app_translator = QTranslator()
    if _load(app_translator, translations_dir(), "app", locale) and app.installTranslator(
        app_translator
    ):
        installed.append(app_translator)

    qt_translator = QTranslator()
    qt_path = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
    if _load(qt_translator, qt_path, "qtbase", locale) and app.installTranslator(qt_translator):
        installed.append(qt_translator)

    return installed
