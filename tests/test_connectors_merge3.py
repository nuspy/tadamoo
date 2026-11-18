"""The three-way merge of a skill folder update: who changed what since the base, and applying the plan."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tadamoo.connectors import merge3        # noqa: E402


def tree(path, files):
    for name, content in files.items():
        p = path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())


def test_every_outcome_with_a_base_to_compare_against(tmp_path):
    base, ours, theirs = tmp_path / "base", tmp_path / "ours", tmp_path / "theirs"
    tree(base, {"untouched.txt": "a", "upstream_changes.txt": "a", "i_change.txt": "a", "both_same_way.txt": "a", "both_differ.txt": "a", "upstream_removes.txt": "a", "i_remove.txt": "a"})
    tree(ours, {"untouched.txt": "a", "upstream_changes.txt": "a", "i_change.txt": "b", "both_same_way.txt": "b", "both_differ.txt": "b", "i_add.txt": "new", "upstream_removes.txt": "a"})
    tree(theirs, {"untouched.txt": "a", "upstream_changes.txt": "b", "i_change.txt": "a", "both_same_way.txt": "b", "both_differ.txt": "c", "upstream_adds.txt": "new", "i_remove.txt": "a"})
    got = merge3.plan(base, ours, theirs)
    assert got == {
        "untouched.txt": merge3.SAME, "upstream_changes.txt": merge3.UPSTREAM, "i_change.txt": merge3.KEPT, "both_same_way.txt": merge3.SAME,
        "both_differ.txt": merge3.CONFLICT, "i_add.txt": merge3.KEPT, "upstream_removes.txt": merge3.UPSTREAM, "upstream_adds.txt": merge3.UPSTREAM,
        "i_remove.txt": merge3.KEPT,
    }


def test_two_independent_new_files_with_the_same_name_is_a_conflict_not_a_guess(tmp_path):
    base, ours, theirs = tmp_path / "base", tmp_path / "ours", tmp_path / "theirs"
    tree(ours, {"helper.py": "mine"})
    tree(theirs, {"helper.py": "upstream's"})
    assert merge3.plan(base, ours, theirs) == {"helper.py": merge3.CONFLICT}


def test_without_a_base_any_difference_is_a_conflict_and_agreement_is_still_same(tmp_path):
    ours, theirs = tmp_path / "ours", tmp_path / "theirs"
    tree(ours, {"same.txt": "a", "differs.txt": "mine"})
    tree(theirs, {"same.txt": "a", "differs.txt": "upstream's"})
    assert merge3.plan(None, ours, theirs) == {"same.txt": merge3.SAME, "differs.txt": merge3.CONFLICT}


def test_changed_text_reads_only_what_an_update_would_actually_add_or_change(tmp_path):
    base, ours, theirs = tmp_path / "base", tmp_path / "ours", tmp_path / "theirs"
    tree(base, {"a.txt": "1", "b.txt": "1"})
    tree(ours, {"a.txt": "1", "b.txt": "1"})
    tree(theirs, {"a.txt": "2", "b.txt": "1"})
    got = merge3.plan(base, ours, theirs)
    assert merge3.changed_text(got, theirs) == {"a.txt": "2"}


def test_apply_writes_upstream_and_resolved_conflicts_leaves_kept_and_removes_what_upstream_dropped(tmp_path):
    base, ours, theirs = tmp_path / "base", tmp_path / "ours", tmp_path / "theirs"
    tree(base, {"a.txt": "1", "gone.txt": "1", "x.txt": "1"})
    tree(ours, {"a.txt": "1", "mine.txt": "mine", "gone.txt": "1", "x.txt": "mine too"})
    tree(theirs, {"a.txt": "2", "x.txt": "theirs too"})
    got = merge3.plan(base, ours, theirs)
    merge3.apply(got, ours, theirs, {"x.txt": "upstream"})
    assert (ours / "a.txt").read_text() == "2"                            # upstream, taken
    assert (ours / "mine.txt").read_text() == "mine"                      # kept, untouched
    assert not (ours / "gone.txt").exists()                               # upstream removed it and the person never touched it
    assert (ours / "x.txt").read_text() == "theirs too"                   # conflict resolved "upstream"


def test_apply_can_keep_a_conflict_and_refuses_nothing_itself(tmp_path):
    base, ours, theirs = tmp_path / "base", tmp_path / "ours", tmp_path / "theirs"
    tree(base, {"x.txt": "1"})
    tree(ours, {"x.txt": "mine"})
    tree(theirs, {"x.txt": "theirs"})
    got = merge3.plan(base, ours, theirs)
    merge3.apply(got, ours, theirs, {"x.txt": "kept"})
    assert (ours / "x.txt").read_text() == "mine"
