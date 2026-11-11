"""Application entry point.

QtPy selects whichever Qt binding is installed (PyQt5/6 or PySide2/6), so the
app runs against whatever the environment provides without extra glue.

Qt's event loop does not return to Python between events, so a plain SIGINT is
usually queued and never handled while the app sits idle. We therefore install a
handler that only records the request, drive the interpreter with a short
heartbeat timer so the handler actually runs, and quit from there. A second
Ctrl-C while the first is being handled force-closes, skipping the guard.
"""

from __future__ import annotations

import logging
import logging.handlers
import signal
import sys

import qdarktheme
from qtpy.QtCore import QSettings, QTimer
from qtpy.QtWidgets import QApplication

from . import i18n, paths
from .window import MainWindow

HEARTBEAT_MS = 200
LOGGER = logging.getLogger("maki_trust_editor")

# The theme names are qdarktheme's own (`dark`, `light`, `auto`), so there is no
# second vocabulary to map: the menu shows "light" and stores `light`, and
# "follow system" stores `auto`. Only the default is ours, and it is dark.
# `auto` asks darkdetect which the OS prefers; when detection returns None
# qdarktheme applies DEFAULT_THEME, while a desktop whose scheme it cannot read
# (KDE with a dark scheme on some setups) reports Light and the app honestly
# renders light, so the menu shows what `auto` currently resolves to.
DEFAULT_THEME = "dark"
THEME_SETTINGS_KEY = "ui/theme_mode"


def load_theme_mode() -> str:
    """The saved theme name, or the default when unset or unusable.

    A name from a newer build (or a hand-edited file) is checked against
    ``qdarktheme.get_themes()`` and rejected, since qdarktheme would raise on it.
    """
    try:
        stored = QSettings().value(THEME_SETTINGS_KEY, DEFAULT_THEME, type=str)
    except Exception:  # noqa: BLE001 - a settings backend failure must not stop the app
        LOGGER.warning("could not read the saved theme, using %s", DEFAULT_THEME, exc_info=True)
        return DEFAULT_THEME
    if stored not in qdarktheme.get_themes():
        LOGGER.info("ignoring unknown saved theme %r, using %s", stored, DEFAULT_THEME)
        return DEFAULT_THEME
    return stored


def save_theme_mode(mode: str) -> None:
    """Remember {mode} across runs. Best effort: a failed write is logged only."""
    try:
        settings = QSettings()
        settings.setValue(THEME_SETTINGS_KEY, mode)
        settings.sync()
    except Exception:  # noqa: BLE001
        LOGGER.warning("could not save the theme mode %r", mode, exc_info=True)


def resolved_theme(mode: str) -> str:
    """The concrete theme {mode} renders to now: ``dark`` or ``light``.

    Only ``auto`` is indirect: it asks darkdetect, and None (detection
    unavailable) means qdarktheme applies DEFAULT_THEME.
    """
    if mode != "auto":
        return mode
    try:
        import darkdetect

        detected = darkdetect.theme()
    except Exception:  # noqa: BLE001
        LOGGER.warning("system theme detection failed, using %s", DEFAULT_THEME, exc_info=True)
        return DEFAULT_THEME
    return detected.lower() if detected else DEFAULT_THEME


def apply_theme(mode: str, *, save: bool = True) -> str:
    """Apply {mode} to the running app and optionally persist it.

    qdarktheme re-sets the application stylesheet and palette on every call, so
    this switches live with no restart. ``auto`` starts a listener that re-themes
    when the OS changes; an explicit name stops it. Returns the name applied.
    """
    if mode not in qdarktheme.get_themes():
        mode = DEFAULT_THEME
    qdarktheme.setup_theme(mode, default_theme=DEFAULT_THEME)
    if save:
        save_theme_mode(mode)
    return mode


def setup_logging() -> None:
    """Log to a rotating file in our own state dir, and nothing else.

    A GUI has no console, so a crash or a failed write would otherwise vanish.
    The handler is best-effort: if the dir or file can't be opened the app still
    runs, just without a log.
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    try:
        paths.app_state_dir().mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            paths.log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)

    def _excepthook(exc_type, exc, tb) -> None:
        LOGGER.critical("uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _excepthook
    LOGGER.info("logging to %s", paths.log_path())


class InterruptController:
    """Turn SIGINT (and SIGBREAK on Windows) into a clean Qt shutdown.

    The signal handler must stay async-signal-safe, so it only sets a flag; the
    heartbeat timer calls :meth:`poll` on the Qt thread, where quitting is safe.
    """

    def __init__(self, app: QApplication) -> None:
        self._app = app
        self._close_target = None  # set by install(); a window or None
        self._pending = 0
        self._previous: dict[int, object] = {}

    def install(self, close_target=None) -> None:
        self._close_target = close_target
        for sig in self._signals():
            self._previous[int(sig)] = signal.getsignal(sig)
            signal.signal(sig, self._on_signal)
        self._timer = QTimer()
        self._timer.setInterval(HEARTBEAT_MS)
        self._timer.timeout.connect(self.poll)
        self._timer.start()

    def _signals(self) -> tuple[int, ...]:
        signals = [signal.SIGINT]
        sigbreak = getattr(signal, "SIGBREAK", None)
        if sigbreak is not None:
            signals.append(sigbreak)
        return tuple(signals)

    def _on_signal(self, _signum: int, _frame: object) -> None:
        # The handler must stay async-signal-safe, so it only counts presses;
        # the heartbeat timer turns them into close attempts on the Qt thread.
        self._pending += 1

    def poll(self) -> None:
        if not self._pending:
            return
        # A second Ctrl-C while the first close prompt is still open is a
        # force: dismiss the guard and shut down. Otherwise just attempt the
        # guarded close once.
        force = self._pending >= 2
        self._pending = 0
        if force and self._close_target is not None:
            self._close_target._force_close = True
        if self._close_target is not None:
            if not self._close_target.close():
                # Cancelled: restore the pre-Ctrl-C state and keep the
                # heartbeat armed so a later Ctrl-C starts over.
                self._close_target._force_close = False
            else:
                self._timer.stop()
            return
        self._timer.stop()
        self._app.quit()

    def restore(self) -> None:
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)


def main() -> int:
    setup_logging()
    app = QApplication(sys.argv)
    # Named before anything reads QSettings: the store path is derived from the
    # organization and application names, so setting them later would land the
    # theme preference in a different file than the one it is read back from.
    app.setOrganizationName("maki")
    app.setApplicationName("maki trust editor")
    apply_theme(load_theme_mode(), save=False)
    # Install translators before any widget is built: a widget captures its text
    # when constructed, so a translator installed afterwards would miss it.
    translators = i18n.install(app)
    LOGGER.info("i18n: %d translator(s) installed", len(translators))
    window = MainWindow()
    interrupt = InterruptController(app)
    interrupt.install(close_target=window)
    window.show()
    try:
        return app.exec()
    except KeyboardInterrupt:
        return 130
    finally:
        interrupt.restore()


if __name__ == "__main__":
    raise SystemExit(main())
