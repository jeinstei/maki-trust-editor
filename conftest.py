"""Shared test configuration.

pytest's ``tmp_path`` follows ``tempfile.gettempdir()``, which probes candidate
directories by creating a randomly named file. A sandbox can allow ordinary
writes to ``/tmp`` while refusing that exact pattern, and ``tempfile`` then falls
back to the current directory — leaving a ``tmp********`` directory in the
project root on every run.

The base is named a subfolder of the project instead, so the litter is contained
on such a machine and harmless on a working one.
"""

from __future__ import annotations

import os

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
LOCAL_TMP_NAME = ".pytest-tmp"


def pytest_configure(config) -> None:
    if config.option.basetemp is None:
        config.option.basetemp = os.path.join(PROJECT_ROOT, LOCAL_TMP_NAME)
