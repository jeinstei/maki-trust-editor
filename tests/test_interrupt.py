"""Tests for the Ctrl-C handling and the unsaved-changes close guard.

These need a QApplication but no window manager: the ``minimal`` platform plugin
gives a usable app object and widget tree without a display.
"""

from __future__ import annotations

import os
import signal

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "minimal")

from qtpy.QtWidgets import QApplication, QMessageBox  # noqa: E402

from maki_trust_editor.app import InterruptController  # noqa: E402
from maki_trust_editor.window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app() -> QApplication:
    instance = QApplication.instance() or QApplication([])
    return instance


@pytest.fixture
def window(app: QApplication) -> MainWindow:
    return MainWindow()


def _controller(app: QApplication, window: MainWindow | None) -> InterruptController:
    controller = InterruptController(app)
    controller.install(close_target=window)
    return controller


def _fake_close(window: MainWindow, result: bool) -> list[bool]:
    calls: list[bool] = []
    window.close = lambda: (calls.append(result), result)[1]
    return calls


def test_no_pending_signal_does_nothing(app: QApplication, window: MainWindow) -> None:
    controller = _controller(app, window)
    calls = _fake_close(window, True)

    controller.poll()

    assert calls == []
    assert controller._timer.isActive()
    controller.restore()


@pytest.mark.parametrize(
    ("presses", "close_result", "with_target", "exp_calls", "exp_active", "exp_force"),
    [
        (1, True, True, [True], False, False),
        (2, True, True, [True], False, True),
        (1, False, True, [False], True, False),
        (1, True, False, None, False, False),
    ],
    ids=["close", "force", "cancel", "no_target"],
)
def test_poll_outcomes(
    app: QApplication,
    window: MainWindow,
    presses: int,
    close_result: bool,
    with_target: bool,
    exp_calls,
    exp_active: bool,
    exp_force: bool,
) -> None:
    controller = _controller(app, window if with_target else None)
    calls = _fake_close(window, close_result)

    for _ in range(presses):
        controller._on_signal(signal.SIGINT, None)
    controller.poll()

    if exp_calls is not None:
        assert calls == exp_calls
    assert controller._pending == 0
    assert controller._timer.isActive() is exp_active
    assert window._force_close is exp_force
    controller.restore()


class _CloseEvent:
    """Minimal stand-in for QCloseEvent, recording accept/ignore."""

    def __init__(self) -> None:
        self.accepted: list[bool] = []

    def accept(self) -> None:
        self.accepted.append(True)

    def ignore(self) -> None:
        self.accepted.append(False)


@pytest.mark.parametrize(
    ("dirty", "force"),
    [(False, False), (True, True)],
    ids=["clean", "force_close"],
)
def test_close_event_accepts_without_prompt(
    window: MainWindow, monkeypatch, dirty: bool, force: bool
) -> None:
    monkeypatch.setattr(window, "_dirty", lambda: dirty)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: pytest.fail("prompt should be skipped")
    )
    window._force_close = force
    event = _CloseEvent()

    window.closeEvent(event)

    assert event.accepted == [True]


def test_close_event_prompts_when_dirty(window: MainWindow, monkeypatch) -> None:
    monkeypatch.setattr(window, "_dirty", lambda: True)
    answers = iter([QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Yes])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: next(answers))
    event = _CloseEvent()

    window.closeEvent(event)
    assert event.accepted == [False]  # Cancel keeps the window open

    window.closeEvent(event)
    assert event.accepted == [False, True]  # Yes closes it
