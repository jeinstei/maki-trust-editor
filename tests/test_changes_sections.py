"""Section-level changes must reach the save dialog.

Scopes are not the whole of a ``permissions.toml`` write: a section can appear or
vanish on its own, and can carry a ``default`` and preserved keys that decide
outcomes. Diffing scopes alone left those writes unannounced — the worst case a
save whose only change was such a section, reported as "nothing to save" and
skipped outright.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maki_trust_editor import changes, permissions

BASE = 'default = "allow"\n[bash]\nallow = ["git *"]\n'


def _lines(tmp_path: Path, edit) -> list[str]:
    """Load a file with ``BASE`` content, let ``edit`` change the model, list it."""
    path = tmp_path / "permissions.toml"
    path.write_text(BASE, encoding="utf-8")
    model = permissions.PermissionsFile.load(path)
    edit(model)
    return [change.line() for change in changes.permissions_changes(path, model).changes]


def _add_empty(model: permissions.PermissionsFile) -> None:
    model.tools["grep"] = permissions.ToolRules(name="grep")


def _add_with_default(model: permissions.PermissionsFile) -> None:
    model.tools["grep"] = permissions.ToolRules(name="grep", default="deny")


def _add_mcp(model: permissions.PermissionsFile) -> None:
    model.mcp["github"] = permissions.McpServerRules(server="github")


def _remove_section(model: permissions.PermissionsFile) -> None:
    del model.tools["bash"]


def _change_default(model: permissions.PermissionsFile) -> None:
    model.tools["bash"].default = "deny"


def _add_unknown(model: permissions.PermissionsFile) -> None:
    model.tools["bash"].unknown["weird"] = 1


def _nothing(model: permissions.PermissionsFile) -> None:
    pass


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (_add_empty, ["+ [grep]"]),
        (_add_with_default, ["+ [grep]  default deny"]),
        (_add_mcp, ["+ [mcp.github]"]),
        (_remove_section, ["- [bash]", "- bash  git *  allow"]),
        (_change_default, ["~ [bash] default  deny  (was None)"]),
        (_add_unknown, ["+ [bash] weird"]),
        (_nothing, []),
    ],
    ids=[
        "add-empty",
        "add-with-default",
        "add-mcp",
        "remove",
        "change-default",
        "add-unknown",
        "none",
    ],
)
def test_section_changes_are_listed(tmp_path: Path, edit, expected) -> None:
    assert _lines(tmp_path, edit) == expected


def test_adding_only_an_empty_section_is_not_reported_as_nothing(tmp_path: Path) -> None:
    """The save gate skips an empty list, so this must never come back empty."""
    path = tmp_path / "permissions.toml"
    path.write_text(BASE, encoding="utf-8")
    model = permissions.PermissionsFile.load(path)
    _add_empty(model)

    review = changes.permissions_changes(path, model)

    assert not review.empty
    assert "[grep]" in model.to_toml()


def test_removing_a_section_reports_the_section_and_its_rules(tmp_path: Path) -> None:
    """A whole section going away names the section and each rule it held.

    Only the section line would be true but unhelpful: the rules vanish with it,
    and the dialog exists to show exactly that.
    """
    path = tmp_path / "permissions.toml"
    path.write_text(BASE, encoding="utf-8")
    model = permissions.PermissionsFile.load(path)
    _remove_section(model)

    lines = [change.line() for change in changes.permissions_changes(path, model).changes]

    assert lines == ["- [bash]", "- bash  git *  allow"]
