"""Round-trip tests for the parsers.

These build their own fixtures in a tmp dir and never touch the real maki
state, so they are safe to run anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import tomlkit

from maki_trust_editor import folders, permissions, safe_write, sessions, workspace

PERMISSIONS_PATH = Path("permissions.toml")


def _parse(content: str) -> permissions.PermissionsFile:
    return permissions.PermissionsFile.from_document(PERMISSIONS_PATH, tomlkit.parse(content))


def test_permissions_round_trips_every_shape() -> None:
    shapes = {
        "native": 'default = "allow"\n\n[bash]\nallow = ["cargo *"]\ndeny = ["rm -rf *"]\n',
        "tool_default": '[bash]\ndefault = "allow"\n',
        "allow_all_bool": "[bash]\nallow = true\n",
        "mcp": '[mcp.github]\ndeny = ["admin_delete"]\n',
        "legacy": "allow_all = true\n",
        "unknown_top_level": 'something = 5\n[bash]\nallow = ["x"]\n',
        "unknown_section_key": '[bash]\nallow = ["cargo *"]\nfuture_key = "keepme"\n',
    }
    for name, content in shapes.items():
        rendered = _parse(content).to_toml()
        assert _parse(rendered).to_toml() == rendered, name


def test_permissions_native_parses_scopes() -> None:
    parsed = _parse('default = "deny"\n[bash]\nallow = ["cargo *"]\ndeny = ["rm -rf *"]\n')
    assert parsed.default == "deny"
    bash = parsed.tools["bash"]
    assert bash.allow == ["cargo *"]
    assert bash.deny == ["rm -rf *"]


def test_permissions_preserves_unknown_top_level() -> None:
    assert "something = 5" in _parse('something = 5\n[bash]\nallow = ["x"]\n').to_toml()


def test_permissions_preserves_section_unknown_keys() -> None:
    parsed = _parse('[bash]\nallow = ["cargo *"]\nfuture_key = "keepme"\n')
    assert parsed.tools["bash"].unknown == {"future_key": "keepme"}
    assert "keepme" in parsed.to_toml()


def test_session_rewrites_only_the_last_line(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    header = {"t": "header", "v": 2, "id": "abc", "cwd": "/p", "model": "m", "created_at": 1}
    # Non-ASCII and a stray regex-looking scope: the prefix must survive byte for
    # byte, so anything maki wrote (including UTF-8 and JSON escaping) is safe.
    msg = {"t": "msg", "d": {"role": "user", "content": [{"type": "text", "text": "héllo ☃"}]}}
    meta = {
        "t": "meta",
        "title": "t",
        "token_usage": {"input_tokens": 5},
        "session_rules": [{"tool": "bash", "scope": "ls *", "effect": "allow"}],
    }
    lines = [json.dumps(x, ensure_ascii=False) for x in (header, msg, meta)]
    original = ("\n".join(lines) + "\n").encode("utf-8")
    path.write_bytes(original)

    session = sessions.Session.load(path)
    assert session.session_id == "abc"
    assert len(session.rules) == 1
    assert session.has_rules_line

    # No rule change: the rendered file is byte-identical.
    assert session.rules_line() == (lines[2] + "\n").encode("utf-8")

    session.rules = [sessions.SessionRule(tool="write", scope="/tmp/**", effect="allow")]
    session.write()

    rewritten = path.read_bytes()
    prefix = ("\n".join(lines[:2]) + "\n").encode("utf-8")
    assert rewritten.startswith(prefix)  # header and message untouched
    last = json.loads(rewritten[len(prefix) :].decode("utf-8"))
    assert last["t"] == "meta"
    assert last["title"] == "t"
    assert last["token_usage"] == {"input_tokens": 5}  # unknown keys preserved
    assert last["session_rules"] == [{"tool": "write", "scope": "/tmp/**", "effect": "allow"}]


def test_session_view_reload_picks_up_a_grown_file(tmp_path: Path) -> None:
    """A live session grows while maki runs, so a cached view must re-read.

    The stale last-line offset would otherwise point into the middle of the file:
    the rules shown would be the old ones and a save would truncate the append.
    """
    path = tmp_path / "live.jsonl"

    def write(rules: list[dict], extras: int) -> None:
        lines = [json.dumps({"t": "header", "id": "live", "cwd": "/x"}, ensure_ascii=False)]
        lines += [json.dumps({"t": "msg", "text": "y" * 200})] * extras
        lines.append(json.dumps({"t": "meta", "session_rules": rules}))
        path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))

    write([{"tool": "bash", "effect": "allow"}], 0)
    view = workspace.SessionView(session=sessions.Session(path=path))
    assert view.reload()
    first_offset = view.session._last_offset
    assert [r.tool for r in view.session.rules] == ["bash"]

    write([{"tool": "edit", "effect": "deny"}], 40)

    assert view.reload()
    assert [r.tool for r in view.session.rules] == ["edit"]
    assert view.session._last_offset > first_offset


def test_folder_trust_removes_folder_and_rejection(tmp_path: Path) -> None:
    path = tmp_path / "trusted-folders.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "folders": [{"path": "/a", "files": ["permissions"]}, {"path": "/b", "files": []}],
                "rejected": ["/c"],
            }
        ),
        encoding="utf-8",
    )
    doc = folders.TrustDocument.load(path)
    assert doc.remove_folder("/a")
    assert doc.remove_rejected("/c")

    reloaded = json.loads(doc.to_json())
    assert reloaded["version"] == 2
    assert reloaded["folders"] == [{"path": "/b", "files": []}]
    assert reloaded["rejected"] == []


def test_safe_write_keeps_a_backup(tmp_path: Path) -> None:
    path = tmp_path / "permissions.toml"
    path.write_text("old\n", encoding="utf-8")
    result = safe_write.save_all([safe_write.WriteTarget(path=path, content="new\n")])
    assert path.read_text() == "new\n"
    assert len(result.backups) == 1
    assert result.backups[0].read_text() == "old\n"


def test_discover_sessions_lists_live_files_only(tmp_path: Path) -> None:
    """Archived continuation chunks must not appear; only top-level live files."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    live_a = sessions_dir / "a.jsonl"
    live_b = sessions_dir / "b.jsonl"
    live_a.write_text("{}\n", encoding="utf-8")
    live_b.write_text("{}\n", encoding="utf-8")

    archive = sessions_dir / "archive" / "a"
    archive.mkdir(parents=True)
    for name in ("1", "2"):
        (archive / f"{name}.jsonl").write_text("{}\n", encoding="utf-8")
    (sessions_dir / "scan_cache.json").write_text("{}\n", encoding="utf-8")

    found = sessions.discover_sessions(sessions_dir)
    names = sorted(p.name for p in found)
    assert names == ["a.jsonl", "b.jsonl"]


@pytest.mark.parametrize("effect", ["allow", "deny"])
def test_tool_group_preserves_scope_less_rule(effect: str) -> None:
    rules = [
        sessions.SessionRule(tool="bash", scope=None, effect=effect),
        sessions.SessionRule(tool="bash", scope="git *", effect="deny"),
    ]
    groups = sessions.ToolGroup.group(rules)
    assert len(groups) == 1
    assert groups[0].all_effect == effect
    assert groups[0].scopes == [("git *", "deny")]
    assert any(r.scope is None and r.effect == effect for r in groups[0].to_rules())


@pytest.mark.parametrize("case", ["missing", "present"])
def test_pre_trust_roots_load_and_roundtrip(tmp_path: Path, case: str) -> None:
    path = tmp_path / "pre-trust-roots.json"
    if case == "present":
        path.write_text(json.dumps(["/b", "/a", "/a"]), encoding="utf-8")

    roots = folders.PreTrustRoots.load(path)
    if case == "missing":
        assert not roots.exists
        assert roots.roots == []
    else:
        assert roots.exists
        assert roots.roots == ["/b", "/a", "/a"]

    assert roots.add("/c")
    assert not roots.add("/c")
    assert roots.remove("/c")
    assert not roots.remove("/c")

    reloaded = json.loads(roots.to_json())
    assert reloaded == sorted(set(roots.roots))
