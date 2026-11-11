"""Decoding a session id for display.

maki names a session with a base58-encoded UUIDv7, which is opaque on screen.
The Sessions header therefore shows the id next to the UUID it decodes to, plus
the creation time the id itself encodes (a UUIDv7 embeds one; a legacy v4 does
not). When the id's time and the header's ``created_at`` disagree, the header
says so.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "minimal")

import pytest  # noqa: E402
from qtpy.QtWidgets import QApplication  # noqa: E402

from maki_trust_editor import sessions  # noqa: E402
from maki_trust_editor.window import SessionsTab  # noqa: E402

# A v7 with a known timestamp: maki-storage's own test sample.
V7_HEX = "01965087-4c71-7f00-8000-000000000000"
# The same 16 bytes, base58-encoded the way maki writes a new id.
V7_BASE58 = "CNK1hV6GWoysH3KQMm5wu"
V7_CREATED_AT = 1745107766  # 2025-04-20 00:09:26 UTC
V7_CREATED_AT_MS = 1745107766.385
V7_STAMP = "2025-04-20 00:09:26 UTC"
# A legacy v4: random, so it carries no timestamp.
V4_HEX = "f72ffa04-3358-4b57-b199-b38a86760aa5"


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def tab(app: QApplication) -> SessionsTab:
    return SessionsTab(
        on_change=lambda: None,
        is_dirty=lambda row: False,
        rebaselined=lambda view: None,
    )


def _session(sid: str, created_at: int = 0) -> sessions.Session:
    session = sessions.Session(
        path=Path("/x.jsonl"), session_id=sid, cwd="/p", model="m", created_at=created_at
    )
    session.rules = [sessions.SessionRule(tool="bash")]
    return session


def test_base58_decodes_to_hex_uuid() -> None:
    decoded = sessions.decode_session_id(V7_BASE58)
    assert decoded is not None
    assert decoded.uuid == V7_HEX
    assert decoded.version == 7


def test_v7_created_time_comes_from_the_id() -> None:
    decoded = sessions.decode_session_id(V7_HEX)
    assert decoded is not None
    assert decoded.created is not None
    assert decoded.created.timestamp() == V7_CREATED_AT_MS


def test_v4_has_no_embedded_time() -> None:
    decoded = sessions.decode_session_id(V4_HEX)
    assert decoded is not None
    assert decoded.version == 4
    assert decoded.created is None


@pytest.mark.parametrize("bad", ["", "hello world", "abc", "0OIl"])
def test_unparseable_id_yields_none(bad: str) -> None:
    assert sessions.decode_session_id(bad) is None


def test_base58_id_is_shown_with_its_uuid(tab: SessionsTab) -> None:
    assert tab._id_line(V7_BASE58) == f"{V7_BASE58} - {V7_HEX} (v7)"


def test_hex_id_is_not_duplicated(tab: SessionsTab) -> None:
    # A legacy hex id already is the UUID, so showing it twice would be noise.
    assert tab._id_line(V4_HEX) == f"{V4_HEX} (v4)"


def test_unparseable_id_is_shown_raw(tab: SessionsTab) -> None:
    assert tab._id_line("not-an-id") == "not-an-id"


def test_created_line_reports_the_ids_time(tab: SessionsTab) -> None:
    assert tab._created_line(V7_HEX, V7_CREATED_AT) == f"created: {V7_STAMP}"


def test_created_line_ignores_the_header_when_it_agrees(tab: SessionsTab) -> None:
    # The id holds milliseconds, the header whole seconds: one second of slack is
    # not a disagreement.
    line = tab._created_line(V7_HEX, V7_CREATED_AT + 1)
    assert line == f"created: {V7_STAMP}"


def test_created_line_flags_a_header_that_differs(tab: SessionsTab) -> None:
    line = tab._created_line(V7_HEX, 1600000000)
    assert line.startswith(f"created: {V7_STAMP}")
    assert "differs from header" in line
    assert tab._utc_stamp(1600000000) in line


def test_created_line_is_none_without_an_embedded_time(tab: SessionsTab) -> None:
    assert tab._created_line(V4_HEX, 1600000000) == "created: (none)"


def test_header_places_the_created_row_above_cwd(tab: SessionsTab) -> None:
    lines = tab._session_header(_session(V7_BASE58, V7_CREATED_AT)).split("\n")
    assert lines[0] == f"{V7_BASE58} - {V7_HEX} (v7)"
    assert lines[1] == f"created: {V7_STAMP}"
    assert lines[2].startswith("cwd:")
