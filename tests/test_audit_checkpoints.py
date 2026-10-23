import json
import threading
from pathlib import Path

import pytest

from harness.audit import AuditLedger
from harness.checkpoints import CheckpointManager
from harness.secrets_broker import SecretsBroker


def test_ledger_detects_a_corrupt_line_instead_of_skipping_it(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    ledger = AuditLedger(path)
    ledger.record("agent", "a", {})
    ledger.record("agent", "b", {})
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(lines[0] + "\n{not json\n", encoding="utf-8")
    ok, why = AuditLedger(path).verify_integrity()
    assert not ok and "Unreadable" in why


def test_ledger_verify_never_raises_on_odd_records(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    path.write_text('{}\n[1, 2]\n"text"\n', encoding="utf-8")
    ok, why = AuditLedger(path).verify_integrity()
    assert not ok and why


def test_ledger_detects_a_truncated_tail(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    ledger = AuditLedger(path)
    for i in range(3):
        ledger.record("agent", "step", {"i": i})
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
    ok, why = AuditLedger(path).verify_integrity()
    assert not ok and "truncated" in why


def test_ledger_is_consistent_under_concurrent_writers(tmp_path: Path):
    ledger = AuditLedger(tmp_path / "audit.jsonl")
    threads = [threading.Thread(target=lambda n=n: [ledger.record("t", "w", {"n": n, "k": k}) for k in range(20)])
               for n in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ledger.records) == 100
    assert ledger.verify_integrity() == (True, None)
    assert AuditLedger(tmp_path / "audit.jsonl").verify_integrity() == (True, None)


def test_ledger_masks_secrets_before_writing(tmp_path: Path):
    broker = SecretsBroker({"TOKEN": "ghp_supersecretvalue"})
    ledger = AuditLedger(tmp_path / "audit.jsonl", redactor=broker.mask_secrets)
    ledger.record("agent", "shell_command", {"command": "curl -H ghp_supersecretvalue x", "nested": ["ghp_supersecretvalue"]})
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "ghp_supersecretvalue" not in raw and "[REDACTED:TOKEN]" in raw
    assert ledger.verify_integrity() == (True, None)


def make_workspace(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "src" / "core").mkdir(parents=True)
    (ws / "src" / "kernels").mkdir(parents=True)
    (ws / "src" / "core" / "layer.cpp").write_text("core v1", encoding="utf-8")
    (ws / "src" / "kernels" / "layer.cpp").write_text("kernels v1", encoding="utf-8")
    (ws / "src" / "core" / "__init__.py").write_text("", encoding="utf-8")
    return ws, CheckpointManager(ws, storage_dir=tmp_path / "cps")


def test_same_file_name_in_two_folders_stays_separate(tmp_path: Path):
    ws, cpm = make_workspace(tmp_path)
    cp = cpm.create()
    (ws / "src" / "core" / "layer.cpp").write_text("core v2", encoding="utf-8")
    (ws / "src" / "kernels" / "layer.cpp").write_text("kernels v2", encoding="utf-8")
    cpm.restore(cp)
    assert (ws / "src" / "core" / "layer.cpp").read_text(encoding="utf-8") == "core v1"
    assert (ws / "src" / "kernels" / "layer.cpp").read_text(encoding="utf-8") == "kernels v1"
    assert (ws / "src" / "core" / "__init__.py").exists()


def test_rewind_is_undoable_through_its_safety_snapshot(tmp_path: Path):
    ws, cpm = make_workspace(tmp_path)
    cp = cpm.create("start")
    (ws / "notes.txt").write_text("written by hand after the checkpoint", encoding="utf-8")
    (ws / "src" / "core" / "layer.cpp").write_text("core v2", encoding="utf-8")
    cpm.restore(cp)
    assert not (ws / "notes.txt").exists()
    cpm.restore(cpm.last_safety_checkpoint)
    assert (ws / "notes.txt").read_text(encoding="utf-8") == "written by hand after the checkpoint"
    assert (ws / "src" / "core" / "layer.cpp").read_text(encoding="utf-8") == "core v2"


def test_rewind_without_deleting_keeps_new_files(tmp_path: Path):
    ws, cpm = make_workspace(tmp_path)
    cp = cpm.create()
    (ws / "new.txt").write_text("keep", encoding="utf-8")
    cpm.restore(cp, delete_new=False)
    assert (ws / "new.txt").exists()


def test_rewind_removes_emptied_folders_but_not_ignored_ones(tmp_path: Path):
    ws, cpm = make_workspace(tmp_path)
    cp = cpm.create()
    (ws / "build" / "out").mkdir(parents=True)
    (ws / "build" / "out" / "a.o").write_text("x", encoding="utf-8")
    (ws / ".git").mkdir()
    (ws / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    cpm.restore(cp)
    assert not (ws / "build").exists()
    assert (ws / ".git" / "HEAD").exists()


def test_a_tampered_checkpoint_cannot_write_outside_the_workspace(tmp_path: Path):
    ws, cpm = make_workspace(tmp_path)
    cp = cpm.create()
    meta_path = tmp_path / "cps" / cp / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["files"].append("../outside.txt")
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    (tmp_path / "cps" / cp / "outside.txt").write_text("evil", encoding="utf-8")
    with pytest.raises(ValueError, match="outside the workspace"):
        cpm.restore(cp)
    assert not (tmp_path / "outside.txt").exists()
    assert (ws / "src" / "core" / "layer.cpp").read_text(encoding="utf-8") == "core v1"


@pytest.mark.parametrize("bad", ["../cp_1_1", "cp_1", "x", "cp_1_1/../../a"])
def test_checkpoint_ids_are_validated(tmp_path: Path, bad):
    _, cpm = make_workspace(tmp_path)
    with pytest.raises(ValueError):
        cpm.restore(bad)


def test_ids_do_not_collide_within_a_millisecond(tmp_path: Path):
    _, cpm = make_workspace(tmp_path)
    ids = {cpm.create() for _ in range(5)}
    assert len(ids) == 5
    assert len(cpm.list_checkpoints()) == 5


def test_storage_inside_the_workspace_is_refused(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(ValueError, match="inside the workspace"):
        CheckpointManager(ws, storage_dir=ws / ".snapshots")


def test_a_workspace_under_a_node_modules_folder_is_still_tracked(tmp_path: Path):
    ws = tmp_path / "node_modules" / "proj"
    ws.mkdir(parents=True)
    (ws / "a.txt").write_text("x", encoding="utf-8")
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps")
    assert json.loads((tmp_path / "cps" / cpm.create() / "meta.json").read_text(encoding="utf-8"))["files"] == ["a.txt"]
