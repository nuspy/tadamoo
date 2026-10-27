"""Which project notes are put before the model: only those that share enough with the request, whatever the language, and the same notes
in every copy of one repository."""
import subprocess
from pathlib import Path

import pytest

from harness.memory import MemoryStore, canonical_workspace, default_db_path, estimate_tokens, query_terms
from harness.stack import AgentConfig, build_agent
from harness.worktrees import Worktrees

BUILD = "The build uses cmake and ninja; configure with -DCMAKE_BUILD_TYPE=Release."
DEPLOY = "Deploy goes to tadamoo.com with the script deploy.ps1, never by hand."
SM = "Target sm_120 needs the flag --arch=native when it is built with CUDA."


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    s.add_memory(BUILD, "build")
    s.add_memory(DEPLOY, "deploy")
    yield s
    s.close()


def contents(notes):
    return [n["content"] for n in notes]


def test_a_request_about_something_else_gets_no_note_because_of_the_word_the(store):
    assert store.relevant("what is the weather like today in the city") == []
    assert store.relevant("the") == [] and store.relevant("che tempo fa oggi nella città") == []
    assert store.relevant("where is the cmake build and the deploy of the thing") != []      # real words still bring notes


def test_common_words_of_english_and_italian_are_not_words_of_the_request():
    assert query_terms("What is the weather like today in the city?") == ["weather", "today", "city"]
    assert query_terms("Qual è il tempo oggi nella città?") == ["tempo", "oggi", "citta"]
    assert query_terms("NEAR AND OR") == [] and query_terms('"') == []


def test_identifiers_keep_their_underscores_dashes_and_dots(store):
    store.add_memory(SM, "build")
    assert query_terms("sm_120 e-mail v1.2.3 --arch=native") == ["sm_120", "e-mail", "v1.2.3", "arch", "native"]
    found = store.relevant("how do I get this to run on sm_120 today?")
    assert contents(found) == [SM]                                                              # one specific word is enough
    assert contents(store.relevant("script deploy.ps1")) == [DEPLOY]


def test_a_note_does_not_need_every_word_of_the_request(store):
    found = store.relevant("how do I build the project for production with cmake and ninja on a slow machine")
    assert contents(found) == [BUILD]


def test_a_single_loose_word_is_not_enough_in_a_long_request(store):
    assert store.relevant("please tell me about the deploy of our quarterly financial figures to the board") == []
    assert contents(store.relevant("deploy")) == [DEPLOY]                                        # a short request that the note covers


def test_the_threshold_can_be_lowered_and_the_number_and_size_of_notes_are_capped(store):
    for i in range(6):
        store.add_memory(f"cmake note number {i} about the build and ninja", "x")
    assert len(store.relevant("build cmake ninja notes", limit=3, min_score=0.5)) == 3
    assert store.relevant("please tell me about the deploy of our quarterly financial figures", min_score=0) != []
    one = store.relevant("build cmake ninja notes", limit=10, min_score=0.5, budget_tokens=estimate_tokens(BUILD) + 5)
    assert len(one) == 1
    cut = store.relevant("build cmake ninja notes", limit=10, min_score=0.5, budget_tokens=10)
    assert len(cut) == 1 and cut[0]["content"].endswith("[...]") and len(cut[0]["content"]) < 60


def test_explicit_searches_still_find_notes_by_any_of_their_words(store):
    store.add_memory(SM, "build")
    assert [n["content"] for n in store.search("how do I build for sm_120 with cuda")][0] == SM
    assert store.search("") == [] and store.search("NEAR AND") == []


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", *args], cwd=str(cwd), capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    (r / "src").mkdir(parents=True)
    git(r, "init", "-q")
    (r / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "first")
    return r


def test_the_copies_of_one_repository_are_one_project(tmp_path, repo, monkeypatch):
    monkeypatch.setenv("BASALT_DATA", str(tmp_path / "data"))
    copies = Worktrees(tmp_path / "wt")
    first, second = copies.create("t1", repo), copies.create("t2", repo)
    assert canonical_workspace(first["workdir"]) == repo.resolve() == canonical_workspace(repo)
    assert default_db_path(first["workdir"]) == default_db_path(second["workdir"]) == default_db_path(repo)
    sub = copies.create("t3", repo / "src")
    assert default_db_path(sub["workdir"]) == default_db_path(repo / "src") != default_db_path(repo)


def test_a_note_kept_in_one_copy_is_found_from_another_and_from_the_repository(tmp_path, repo, monkeypatch):
    monkeypatch.setenv("BASALT_DATA", str(tmp_path / "data"))
    copies = Worktrees(tmp_path / "wt")
    first, second = copies.create("t1", repo), copies.create("t2", repo)
    made = []
    for path in (first["workdir"], second["workdir"], str(repo)):
        built = build_agent(AgentConfig(workspace=str(path), use_checkpoints=False, data_dir=None), client=object())
        made.append(built)
    try:
        made[0].agent.tools.execute("remember", {"content": SM})
        for built in made[1:]:
            found = built.agent.tools.execute("recall", {"query": "sm_120"})
            assert [n["content"] for n in found] == [SM]
        assert "sm_120" in made[2].retrieved_message("how do I build for sm_120")["content"]
        assert made[2].retrieved_message("what is the weather like today in the city") is None
    finally:
        for built in made:
            built.close()


def test_other_folders_keep_their_own_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("BASALT_DATA", str(tmp_path / "data"))
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert canonical_workspace(a) == a.resolve() and default_db_path(a) != default_db_path(b)
    (a / ".git").mkdir()
    assert canonical_workspace(a) == a.resolve()
