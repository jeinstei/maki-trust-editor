"""What the save dialog lists, as a multiset of rules.

The list is the only thing standing between the user and a write, so each
marker is pinned here: an addition, a removal, an effect flip, and the two
cases where a naive diff says nothing (a duplicate added, a rule reordered).
"""

from __future__ import annotations

import pytest

from maki_trust_editor import changes, sessions

Rule = sessions.SessionRule


def _lines(old: list[sessions.SessionRule], new: list[sessions.SessionRule]) -> list[str]:
    return [change.line() for change in changes._diff_rules(old, new)]


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ([], [Rule("bash", "git *", "allow")], ["+ bash  git *  allow"]),
        ([Rule("bash", "git *", "allow")], [], ["- bash  git *  allow"]),
        (
            [Rule("bash", "git *", "allow")],
            [Rule("bash", "git *", "deny")],
            ["~ bash  git *  deny  (was allow)"],
        ),
        ([], [Rule("read", None, "allow")], ["+ read  (all scopes)  allow"]),
        (
            [Rule("bash", "git *", "allow")],
            [Rule("bash", "git *", "allow")],
            [],
        ),
        (
            [Rule("a", "s1", "allow"), Rule("b", "s2", "deny")],
            [Rule("b", "s2", "deny"), Rule("a", "s1", "allow")],
            [],
        ),
    ],
    ids=["add", "remove", "flip", "all-scopes-rule", "no-change", "reorder"],
)
def test_diff_rules_reports_each_kind_of_change(old, new, expected) -> None:
    assert _lines(old, new) == expected


def test_diff_rules_counts_duplicates() -> None:
    """A second copy of an identical rule is an addition, not a no-op.

    Keying on (tool, scope) alone would collapse the two and report nothing,
    while the dirty check (a multiset) says the model changed.
    """
    old = [Rule("bash", "git *", "allow")]
    new = [Rule("bash", "git *", "allow"), Rule("bash", "git *", "allow")]

    assert _lines(old, new) == ["+ bash  git *  allow"]


def test_diff_rules_lists_something_for_every_dirty_state() -> None:
    """The dialog and the dirty check must agree on whether anything changed."""
    pairs = [
        ([], [Rule("x", "y", "allow")]),
        ([Rule("x", "y", "allow")], []),
        ([Rule("x", "y", "allow")], [Rule("x", "y", "allow")] * 3),
        ([Rule("x", "y", "allow"), Rule("x", "y", "deny")], [Rule("x", "y", "deny")]),
        ([Rule("x", "y", "deny")], [Rule("x", "y", "allow"), Rule("x", "y", "deny")]),
    ]
    for old, new in pairs:
        dirty = sessions.rule_set(old) != sessions.rule_set(new)
        assert dirty, "fixture should be dirty"
        assert _lines(old, new), f"dirty but nothing listed: {old} -> {new}"


def test_rule_set_ignores_order_and_counts_duplicates() -> None:
    """The dirty check compares a multiset: order is free, a copy is not."""
    one = [Rule("bash", "git *", "allow"), Rule("read", None, "deny")]
    reordered = [Rule("read", None, "deny"), Rule("bash", "git *", "allow")]

    assert sessions.rule_set(one) == sessions.rule_set(reordered)
    assert sessions.rule_set(one) != sessions.rule_set(one + one[:1])


def test_rule_set_separates_effects() -> None:
    """A flipped effect is a different rule, or a contradiction would read as none."""
    allow = [Rule("bash", "git *", "allow")]
    deny = [Rule("bash", "git *", "deny")]

    assert sessions.rule_set(allow) != sessions.rule_set(deny)
