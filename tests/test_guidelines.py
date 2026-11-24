"""Instruction files in cascade: the person's own, then one per folder down to the working folder; the technical session removes what is said twice."""
from pathlib import Path

import pytest

from tadamoo import guidelines, side_uses


@pytest.fixture
def tree(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / ".git").mkdir()
    data = tmp_path / "data"
    data.mkdir()
    return repo, repo / "pkg", data


class Session:
    def __init__(self, answer="MERGED", fail=False):
        self.answer, self.fail, self.calls = answer, fail, []

    def chat_completion(self, messages, **kw):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("down")
        return {"choices": [{"message": {"content": self.answer}}]}


def test_the_files_come_general_first_and_stop_at_the_repository_root(tree, tmp_path):
    repo, pkg, data = tree
    (tmp_path / "AGENTS.md").write_text("above the repository", encoding="utf-8")
    (data / "AGENTS.md").write_text("mine", encoding="utf-8")
    (repo / "AGENTS.md").write_text("repo", encoding="utf-8")
    (pkg / "CLAUDE.md").write_text("pkg", encoding="utf-8")
    found = guidelines.files(pkg, data)
    assert [p.parent.name for p in found] == ["data", "repo", "pkg"] and found[2].name == "CLAUDE.md"
    assert guidelines.files(pkg, None) == found[1:]


def test_one_file_is_read_as_it_is_and_several_are_joined_under_their_paths_without_a_session(tree):
    repo, pkg, data = tree
    (repo / "AGENTS.md").write_text("Be precise.", encoding="utf-8")
    items, cut = guidelines.read(guidelines.files(pkg, data))
    assert cut == 0 and guidelines.merged(items) == "Be precise."
    (pkg / "AGENTS.md").write_text("Use pytest.", encoding="utf-8")
    items, _ = guidelines.read(guidelines.files(pkg, data))
    text = guidelines.merged(items)
    assert text.index("Be precise.") < text.index("Use pytest.") and "AGENTS.md" in text


def test_the_session_merges_once_and_the_answer_is_kept_by_the_hash_of_the_files(tree, monkeypatch):
    repo, pkg, data = tree
    monkeypatch.setattr(side_uses, "enabled", lambda use: True)
    monkeypatch.setattr(guidelines, "side_extra_for", lambda client, use, size, answer: {"cache_slot": "side"})
    (repo / "AGENTS.md").write_text("Reply in Italian. Use small commits. Reply in Italian.", encoding="utf-8")
    (pkg / "AGENTS.md").write_text("Use small commits. Run pytest.", encoding="utf-8")
    items, _ = guidelines.read(guidelines.files(pkg, data))
    plain = guidelines.joined(items)
    session = Session("Reply in Italian. Use small commits. Run pytest.")
    assert guidelines.merged(items, session, data) == "Reply in Italian. Use small commits. Run pytest."
    assert "repo" in session.calls[0][1]["content"] and session.calls[0][0]["role"] == "system"
    assert guidelines.merged(items, Session(fail=True), data) == "Reply in Italian. Use small commits. Run pytest."     # kept: no new call
    (pkg / "AGENTS.md").write_text("Use small commits. Run pytest -q.", encoding="utf-8")
    again, _ = guidelines.read(guidelines.files(pkg, data))
    assert guidelines.merged(again, Session(fail=True), data) == guidelines.joined(again) != plain       # a changed file is read again


def test_a_merge_that_is_not_shorter_or_a_failed_call_leaves_the_files_as_they_are(tree, monkeypatch):
    repo, pkg, data = tree
    monkeypatch.setattr(side_uses, "enabled", lambda use: True)
    monkeypatch.setattr(guidelines, "side_extra_for", lambda client, use, size, answer: {"cache_slot": "side"})
    (repo / "AGENTS.md").write_text("A.", encoding="utf-8")
    (pkg / "AGENTS.md").write_text("B.", encoding="utf-8")
    items, _ = guidelines.read(guidelines.files(pkg, data))
    assert guidelines.merged(items, Session("x" * 500), data) == guidelines.joined(items)
    assert guidelines.merged(items, Session(fail=True), data) == guidelines.joined(items)
    assert guidelines.merged(items, None, data) == guidelines.joined(items)
