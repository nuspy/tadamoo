import json
import sys
import threading
from pathlib import Path

import pytest

from harness.audit import AuditLedger
from harness.checkpoints import CheckpointManager
from harness.secrets_broker import SecretsBroker
from harness.tools.registry import EffectClass


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
    (ws / "artifacts" / "out").mkdir(parents=True)
    (ws / "artifacts" / "out" / "a.o").write_text("x", encoding="utf-8")
    (ws / ".git").mkdir()
    (ws / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    cpm.restore(cp)
    assert not (ws / "artifacts").exists()
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


def _loop_with_audit(tmp_path, steps, **kw):
    from test_agent_loop import ScriptedClient
    from harness.agent_loop import AgentLoop
    from harness.tools import create_standard_tool_registry
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    registry, _ = create_standard_tool_registry(ws)
    audit = kw.pop("audit", None) or AuditLedger(tmp_path / "audit.jsonl")
    return AgentLoop(ScriptedClient(*steps), registry, audit=audit, **kw), audit


def test_a_tool_call_leaves_a_hash_a_short_masked_preview_the_decision_the_outcome_and_the_time(tmp_path: Path):
    import hashlib
    from test_agent_loop import call, say
    from harness.permissions import PermissionLevel, PermissionManager
    body = "line of a long file " * 400
    secret = "ghp_" + "x" * 70
    broker = SecretsBroker({"TOKEN": secret})
    audit = AuditLedger(tmp_path / "audit.jsonl", redactor=broker.mask_secrets)
    args = {"path": "big.txt", "content": body + secret}
    agent, _ = _loop_with_audit(tmp_path, [call("write_file", args), say("done")], audit=audit,
                                permissions=PermissionManager(level=PermissionLevel.DEFAULT))
    agent.run("go")
    row = next(r for r in audit.records if r["action"] == "tool_executed")["details"]
    masked = json.dumps({"path": "big.txt", "content": body + "[REDACTED:TOKEN]"}, sort_keys=True, ensure_ascii=False)
    assert row["tool"] == "write_file" and row["args_sha256"] == hashlib.sha256(masked.encode("utf-8")).hexdigest()
    assert row["permission"] == "allowed" and row["outcome"] == "ok" and row["effect"] == "reversible" and row["duration_ms"] >= 0
    assert row["arguments"]["path"] == "big.txt" and len(row["arguments"]["content"]) < 100 and "chars]" in row["arguments"]["content"]
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "line of a long file line of a long file line of a long file line of a long file" not in raw and secret not in raw
    assert audit.verify_integrity() == (True, None)


def test_the_outcome_says_ok_error_refused_or_cancelled(tmp_path: Path):
    import threading
    from test_agent_loop import call, say
    from harness.permissions import PermissionLevel, PermissionManager
    cancel = threading.Event()
    steps = [call("write_file", {"path": "a.txt", "content": "A"}, "1"), call("read_file", {"path": "missing.txt"}, "2"),
             call("shell_command", {"command": "echo hi"}, "3"), call("rm_all", {}, "4"), say("done")]
    agent, audit = _loop_with_audit(tmp_path, steps, permissions=PermissionManager(level=PermissionLevel.DEFAULT), cancel=cancel)
    agent.run("go")
    seen = {r["details"]["tool"]: (r["action"], r["details"]["outcome"], r["details"]["permission"])
            for r in audit.records if r["action"] in ("tool_executed", "tool_error", "permission_denied", "tool_invalid", "tool_cancelled")}
    assert seen["write_file"] == ("tool_executed", "ok", "allowed")
    assert seen["read_file"] == ("tool_error", "error", "allowed")
    assert seen["shell_command"] == ("permission_denied", "refused", "denied")
    assert seen["rm_all"][1] == "refused"
    two_calls = {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "s", "type": "function", "function": {"name": "stop_now", "arguments": "{}"}},
        {"id": "w", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "c", "content": "C"})}}]}}]}
    # a tool still running when Stop is pressed ends as cancelled
    agent2, audit2 = _loop_with_audit(tmp_path / "two", [call("shell_command", {"command": f"\"{sys.executable}\" -c \"import time; time.sleep(60)\""}), say("x")],
                                      permissions=PermissionManager(level=PermissionLevel.AUTO), cancel=cancel)
    threading.Timer(0.6, cancel.set).start()
    agent2.run("go")
    assert [r["details"]["outcome"] for r in audit2.records if r["action"] == "tool_cancelled"] == ["cancelled"]
    # a call still waiting when Stop is pressed never runs, and says so
    pre = threading.Event()
    agent3, audit3 = _loop_with_audit(tmp_path / "three", [two_calls, say("x")], cancel=pre)
    agent3.tools.register("stop_now", "stops", {"type": "object", "properties": {}}, EffectClass.PURE, lambda: pre.set() or "stopped")
    agent3.run("go")
    assert [r["details"]["tool"] for r in audit3.records if r["action"] == "tool_cancelled"] == ["write_file"]


def test_rows_written_with_the_arguments_in_full_still_verify_next_to_the_new_ones(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    old = AuditLedger(path)
    old.record("agent", "tool_executed", {"tool": "write_file", "arguments": {"path": "a", "content": "all of it " * 50}, "effect": "reversible"})
    old.record("agent", "permission_granted", {"tool": "write_file", "reason": "ok"})
    again = AuditLedger(path)
    again.record("agent", "tool_executed", again.call_details("write_file", {"path": "b", "content": "new"}, permission="allowed", outcome="ok", duration_s=0.01))
    assert AuditLedger(path).verify_integrity() == (True, None) and len(AuditLedger(path).records) == 3


def test_verify_reports_a_damaged_head_and_every_bad_line_instead_of_raising_or_skipping(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    ledger = AuditLedger(path)
    for i in range(4):
        ledger.record("agent", "step", {"i": i})
    for head in ("[]", '"x"', "null", "7"):
        path.with_suffix(".jsonl.head").write_text(head, encoding="utf-8")
        ok, why = AuditLedger(path).verify_integrity()
        assert not ok and "head file" in why
    path.with_suffix(".jsonl.head").unlink()
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], "{broken", lines[2], '["not", "a", "record"]']) + "\n", encoding="utf-8")
    led = AuditLedger(path)
    found = led.problems()
    assert sum("Unreadable" in p for p in found) == 2 and "Hash chain broken at index 2" not in "".join(found[:1])
    ok, why = led.verify_integrity()
    assert not ok and "Unreadable record at position 1" in why and "more problem" in why
    path.write_bytes(path.read_bytes() + b'\xff\xfe not utf-8\n')                 # bytes that are not text: still a result, never an exception
    assert not AuditLedger(path).verify_integrity()[0]
