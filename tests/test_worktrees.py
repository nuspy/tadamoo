"""A copy of a git folder for a task: made from the folder as it is, changes listed and applied back as staged changes, thrown away whole."""
import subprocess
from pathlib import Path

import pytest

from harness.worktrees import WorktreeError, Worktrees, repo_root


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", *args], cwd=str(cwd), capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    (r / "src").mkdir(parents=True)
    git(r, "init", "-q")
    (r / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    (r / "src" / "b.py").write_text("print('b')\n", encoding="utf-8")
    (r / "gone.txt").write_text("bye\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "first")
    return r


@pytest.fixture
def wt(tmp_path):
    return Worktrees(tmp_path / "data" / "worktrees")


def test_a_copy_starts_from_the_folder_as_it_is_now(repo, wt):
    (repo / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")                  # changed, not committed
    info = wt.create("t1", repo)
    assert (Path(info["dir"]) / "a.txt").read_text(encoding="utf-8") == "one\ntwo\nthree\n"
    assert info["branch"] == "basalt/t1" and Path(info["workdir"]) == Path(info["dir"])
    assert wt.changes(info) == []                                                         # what was already there is not the task's change
    assert (repo / "a.txt").read_text(encoding="utf-8") == "one\ntwo\nthree\n" and "basalt/t1" in git(repo, "branch")


def test_a_folder_inside_the_repository_gets_the_same_folder_in_the_copy(repo, wt):
    info = wt.create("t2", repo / "src")
    assert Path(info["workdir"]) == Path(info["dir"]) / "src" and (Path(info["workdir"]) / "b.py").exists()


def test_what_the_task_changed_is_listed_with_its_diff_and_the_agents_own_folder_is_left_out(repo, wt):
    info = wt.create("t3", repo)
    d = Path(info["dir"])
    (d / "a.txt").write_text("one\nTWO\n", encoding="utf-8")
    (d / "new.txt").write_text("fresh\n", encoding="utf-8")
    (d / "gone.txt").unlink()
    (d / ".basalt" / "artifacts").mkdir(parents=True)
    (d / ".basalt" / "artifacts" / "x.txt").write_text("big output", encoding="utf-8")
    got = {c["path"]: c["status"] for c in wt.changes(info)}
    assert got == {"a.txt": "modified", "new.txt": "added", "gone.txt": "deleted"}
    assert "+TWO" in wt.diff(info, "a.txt") and "-two" in wt.diff(info, "a.txt") and "+fresh" in wt.diff(info, "new.txt")


def test_applying_puts_the_changes_in_the_persons_folder_as_staged_changes_and_goes_on_from_there(repo, wt):
    info = wt.create("t4", repo)
    d = Path(info["dir"])
    (d / "a.txt").write_text("one\nTWO\n", encoding="utf-8")
    (d / "new.txt").write_text("fresh\n", encoding="utf-8")
    (d / "gone.txt").unlink()
    done = wt.apply(info)
    assert sorted(done["applied"]) == ["a.txt", "gone.txt", "new.txt"]
    assert (repo / "a.txt").read_text(encoding="utf-8") == "one\nTWO\n" and (repo / "new.txt").exists() and not (repo / "gone.txt").exists()
    status = git(repo, "status", "--porcelain")
    assert "M  a.txt" in status and "A  new.txt" in status and "D  gone.txt" in status          # staged, not committed
    assert git(repo, "log", "--oneline").count("\n") == 1
    assert wt.changes(info) == [] and wt.apply(info)["applied"] == []                       # nothing new: nothing to apply
    (d / "a.txt").write_text("one\nTWO\nmore\n", encoding="utf-8")
    assert [c["path"] for c in wt.changes(info)] == ["a.txt"]


def test_only_the_chosen_files_are_applied_and_the_rest_stays_held(repo, wt):
    info = wt.create("t5", repo)
    d = Path(info["dir"])
    (d / "a.txt").write_text("changed\n", encoding="utf-8")
    (d / "new.txt").write_text("fresh\n", encoding="utf-8")
    assert wt.apply(info, ["new.txt"])["applied"] == ["new.txt"]
    assert (repo / "new.txt").exists() and (repo / "a.txt").read_text(encoding="utf-8") == "one\ntwo\n"
    assert [c["path"] for c in wt.changes(info)] == ["a.txt"]                              # a.txt is still held
    wt.apply(info, ["a.txt"])
    assert (repo / "a.txt").read_text(encoding="utf-8") == "changed\n" and wt.changes(info) == []


def test_a_change_that_does_not_fit_stops_the_apply_and_nothing_is_half_done(repo, wt):
    info = wt.create("t6", repo)
    d = Path(info["dir"])
    (d / "a.txt").write_text("one\nTASK\n", encoding="utf-8")
    (d / "new.txt").write_text("fresh\n", encoding="utf-8")
    (repo / "a.txt").write_text("one\nPERSON\n", encoding="utf-8")                          # the person changed the same line meanwhile
    with pytest.raises(WorktreeError) as e:
        wt.apply(info)
    assert e.value.code == "conflict" and "a.txt" in str(e.value)
    assert (repo / "a.txt").read_text(encoding="utf-8") == "one\nPERSON\n" and not (repo / "new.txt").exists()
    assert {c["path"] for c in wt.changes(info)} == {"a.txt", "new.txt"}                    # still held, nothing lost


def test_throwing_a_copy_away_removes_its_folder_and_branch_and_the_persons_folder_is_untouched(repo, wt):
    info = wt.create("t7", repo)
    (Path(info["dir"]) / "a.txt").write_text("x\n", encoding="utf-8")
    wt.discard(info)
    assert not Path(info["dir"]).exists() and "basalt/t7" not in git(repo, "branch")
    assert (repo / "a.txt").read_text(encoding="utf-8") == "one\ntwo\n" and "t7" not in git(repo, "worktree", "list")
    wt.discard(info)                                                                      # twice is harmless


def test_a_lost_folder_comes_back_from_its_branch(repo, wt):
    info = wt.create("t8", repo)
    d = Path(info["dir"])
    (d / "a.txt").write_text("kept\n", encoding="utf-8")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "wip")
    import shutil
    shutil.rmtree(d)
    wt.ensure(info)
    assert (d / "a.txt").read_text(encoding="utf-8") == "kept\n"


def test_what_cannot_be_copied_is_said(tmp_path, repo, wt):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError) as e:
        wt.create("t9", plain)
    assert e.value.code == "not_git" and repo_root(plain) is None and repo_root(repo) == repo.resolve()
    with pytest.raises(WorktreeError) as e:
        wt.create("../x", repo)
    assert e.value.code == "bad_id"
    wt.create("t10", repo)
    with pytest.raises(WorktreeError) as e:
        wt.create("t10", repo)
    assert e.value.code == "exists"
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    with pytest.raises(WorktreeError) as e:
        wt.create("t11", empty)
    assert e.value.code == "no_commit"
