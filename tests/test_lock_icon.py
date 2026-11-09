"""The session-open marker renders a lock or a dot, never empty when locked."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "minimal")

from qtpy.QtWidgets import QApplication  # noqa: E402

from maki_trust_editor import window  # noqa: E402


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_unlocked_has_no_prefix(app: QApplication) -> None:
    assert window._lock_prefix(False) == ""


def test_locked_prefix_is_icon_plus_space(app: QApplication) -> None:
    prefix = window._lock_prefix(True)
    assert prefix in (window.LOCK_ICON + " ", window.LOCK_ICON_FALLBACK + " ")
