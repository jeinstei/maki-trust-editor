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

from qtpy.QtCore import QTimer
from qtpy.QtWidgets import QApplication

from . import paths
from .window import MainWindow

HEARTBEAT_MS = 200
LOGGER = logging.getLogger("maki_trust_editor")


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
    app.setApplicationName("maki trust editor")
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
