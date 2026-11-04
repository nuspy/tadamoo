"""Checkpoints keep the previous content of the files an action names, from any folder, with no size or total limit; the setting
has three positions; the loop applies it."""
import json
import shutil
import threading
from pathlib import Path

import pytest

from tadamoo.agent_loop import AgentLoop
from tadamoo.audit import AuditLedger
from tadamoo.checkpoints import CheckpointManager, call_paths, read_mode, resolve_mode, shell_paths, write_mode
from tadamoo.permissions import PermissionLevel, PermissionManager
from tadamoo.tools import create_standard_tool_registry
from test_agent_loop import ScriptedClient, call, say


def put(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    put(root / "src" / "main.py", "print(1)")
    return root


def test_no_checkpoint_is_ever_rotated_away(ws, tmp_path):
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps")
    big = put(tmp_path / "big.bin", "x" * 1000)
    ids = [cpm.create(f"n{i}", [big]) for i in range(30)]
    assert [m["checkpoint_id"] for m in cpm.list_checkpoints()] == ids


@pytest.mark.parametrize("entry", ["../outside.txt", "", "/etc/passwd\x00", "relative/name.txt"])
def test_damaged_entries_are_refused_before_anything_is_touched(ws, tmp_path, entry):
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps")
    main = ws / "src" / "main.py"
    cp = cpm.create(paths=[main])
    meta_path = cpm.storage / cp / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["entries"].append({"path": entry, "existed": False, "stored": "", "bytes": 0})
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    main.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError):
        cpm.restore(cp)
    assert main.read_text(encoding="utf-8") == "changed"


def test_an_entry_without_its_copy_is_refused_before_anything_is_touched(ws, tmp_path):
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps")
    main, other = ws / "src" / "main.py", put(ws / "src" / "other.py", "o")
    cp = cpm.create(paths=[main, other])
    next((cpm.storage / cp / "files").iterdir()).unlink()
    main.write_text("changed", encoding="utf-8")
    other.write_text("changed", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        cpm.restore(cp)
    assert main.read_text(encoding="utf-8") == "changed" and other.read_text(encoding="utf-8") == "changed"


def test_two_managers_on_one_storage_never_take_the_same_id(ws, tmp_path, monkeypatch):
    monkeypatch.setattr("tadamoo.checkpoints.time.time", lambda: 1_700_000_000.0)       # the same millisecond for everyone
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


def test_a_failed_checkpoint_leaves_nothing_behind(ws, tmp_path, monkeypatch):
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps")
    monkeypatch.setattr("tadamoo.checkpoints.shutil.copy2", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        cpm.create(paths=[ws / "src" / "main.py"])
    assert list((tmp_path / "cps").iterdir()) == []


# -- the setting --------------------------------------------------------------------------------------------------


def test_the_setting_defaults_follow_the_permission_level_and_a_choice_wins(tmp_path):
    f = tmp_path / "s.json"
    assert read_mode(f) == ""
    assert resolve_mode("", "default") == "on" and resolve_mode("", "auto") == "soft"
    for mode in ("on", "soft", "off"):
        assert write_mode(mode, f) == mode and read_mode(f) == mode
        assert resolve_mode(read_mode(f), "auto") == mode
    assert write_mode("default", f) == "" and read_mode(f) == ""
    with pytest.raises(ValueError):
        write_mode("sometimes", f)


def test_a_manager_reads_the_setting_each_time_it_is_asked(ws, tmp_path):
    f = tmp_path / "s.json"
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps", settings=f)
    assert cpm.effective_mode("default") == "on" and cpm.effective_mode("auto") == "soft"
    write_mode("off", f)
    assert cpm.effective_mode("auto") == "off"
    assert CheckpointManager(ws, storage_dir=tmp_path / "c2", mode="soft", settings=f).effective_mode("default") == "soft"


# -- which files an action names ----------------------------------------------------------------------------------


def test_file_tools_name_their_path_whether_or_not_it_exists(ws):
    assert call_paths(ws, "write_file", {"path": "new/a.txt", "content": ""}) == [(ws / "new" / "a.txt").resolve()]
    assert call_paths(ws, "edit_file", {"path": "src/main.py"}) == [(ws / "src" / "main.py").resolve()]
    assert call_paths(ws, "list_directory", {"path": "."}) == []


def test_a_shell_command_names_existing_files_quoted_globbed_and_redirected(ws, tmp_path):
    put(ws / "my file.txt")
    put(ws / "a.log")
    put(ws / "b.log")
    elsewhere = put(tmp_path / "other" / "c.cfg")
    command = f'type "my file.txt" & del *.log ; echo hi > out.txt 2>nul ; tool --config="{elsewhere}" ; ls src ; git status > /dev/null'
    names = {p.name for p in shell_paths(ws, command)}
    assert names == {"my file.txt", "a.log", "b.log", "out.txt", "c.cfg"}      # a folder, a command word and the null device are not files


def test_a_redirect_over_an_existing_file_keeps_that_file(ws):
    assert shell_paths(ws, "echo new >> src/main.py") == [(ws / "src" / "main.py").resolve()]


def test_a_shell_checkpoint_puts_a_named_file_back_and_removes_a_redirect_target(ws, tmp_path):
    main = ws / "src" / "main.py"
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps")
    cp = cpm.create("before shell_command", call_paths(ws, "shell_command", {"command": "echo x > made.txt && sed -i s/1/2/ src/main.py"}))
    main.write_text("print(2)", encoding="utf-8")
    put(ws / "made.txt")
    cpm.restore(cp)
    assert main.read_text(encoding="utf-8") == "print(1)" and not (ws / "made.txt").exists()


# -- the loop -----------------------------------------------------------------------------------------------------


def loop(ws, tmp_path, steps, level=PermissionLevel.DEFAULT, mode=None, audit=True):
    registry, _ = create_standard_tool_registry(ws)
    cps = CheckpointManager(ws, storage_dir=tmp_path / "cps", mode=mode, settings=tmp_path / "none.json")
    ledger = AuditLedger(tmp_path / "audit.jsonl") if audit else None
    agent = AgentLoop(client=ScriptedClient(*steps), tool_registry=registry, permissions=PermissionManager(level=level), audit=ledger, checkpoints=cps)
    return agent, cps, ledger


def break_checkpoints(cps, monkeypatch):
    def fail(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(cps, "create", fail)


def test_a_batch_of_calls_shares_one_checkpoint_holding_each_changed_file_once(ws, tmp_path):
    msg = {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "src/main.py", "content": "2"})}},
        {"id": "b", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "src/main.py", "content": "3"})}},
        {"id": "c", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "new.txt", "content": "n"})}}]}}]}
    agent, cps, _ = loop(ws, tmp_path, [msg, say("ok")])
    agent.run("go")
    (only,) = cps.list_checkpoints()
    assert [(Path(e["path"]).name, e["existed"]) for e in only["entries"]] == [("main.py", True), ("new.txt", False)]
    cps.restore(only["checkpoint_id"])
    assert (ws / "src" / "main.py").read_text(encoding="utf-8") == "print(1)" and not (ws / "new.txt").exists()


def test_the_manual_position_refuses_the_action_when_no_checkpoint_can_be_taken(ws, tmp_path, monkeypatch):
    agent, cps, ledger = loop(ws, tmp_path, [call("write_file", {"path": "a.txt", "content": "A"}), say("ok")])
    break_checkpoints(cps, monkeypatch)
    agent.run("go")
    assert not (ws / "a.txt").exists()
    assert any(r["action"] == "tool_blocked" for r in ledger.records)


def test_the_automatic_position_runs_the_action_records_the_failure_and_tells_the_model(ws, tmp_path, monkeypatch):
    client_steps = [call("write_file", {"path": "a.txt", "content": "A"}), say("ok")]
    agent, cps, ledger = loop(ws, tmp_path, client_steps, level=PermissionLevel.AUTO)
    break_checkpoints(cps, monkeypatch)
    agent.run("go")
    assert (ws / "a.txt").read_text(encoding="utf-8") == "A"
    failed = [r for r in ledger.records if r["action"] == "checkpoint_failed"]
    assert len(failed) == 1 and "disk full" in failed[0]["details"]["error"]
    told = next(m for m in agent.client.sent[-1] if m["role"] == "tool")["content"]
    assert "No checkpoint could be taken" in told and "disk full" in told


def test_a_chosen_position_beats_the_permission_level(ws, tmp_path, monkeypatch):
    agent, cps, _ = loop(ws, tmp_path, [call("write_file", {"path": "a.txt", "content": "A"}), say("ok")], level=PermissionLevel.AUTO, mode="on")
    break_checkpoints(cps, monkeypatch)
    agent.run("go")
    assert not (ws / "a.txt").exists()
    agent, cps, _ = loop(ws, tmp_path / "again", [call("write_file", {"path": "b.txt", "content": "B"}), say("ok")], mode="soft")
    break_checkpoints(cps, monkeypatch)
    agent.run("go")
    assert (ws / "b.txt").exists()


def test_the_off_position_takes_no_checkpoint(ws, tmp_path):
    agent, cps, _ = loop(ws, tmp_path, [call("write_file", {"path": "a.txt", "content": "A"}), say("ok")], mode="off")
    agent.run("go")
    assert (ws / "a.txt").exists() and cps.list_checkpoints() == []


# -- going back over several checkpoints ----------------------------------------------------------------------------


def test_going_back_undoes_the_later_checkpoints_too_each_file_to_its_earliest_state(ws, tmp_path):
    main, other = ws / "src" / "main.py", ws / "other.txt"
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps")
    first = cpm.create("first", [main])
    main.write_text("two", encoding="utf-8")
    second = cpm.create("second", [main, other])
    main.write_text("three", encoding="utf-8")
    put(other, "made")
    cpm.restore(second)
    assert main.read_text(encoding="utf-8") == "two" and not other.exists()          # the first checkpoint is older: not undone
    cpm.restore(first)
    assert main.read_text(encoding="utf-8") == "print(1)"


def test_a_restore_leaves_a_file_that_already_has_the_kept_content_alone(ws, tmp_path, monkeypatch):
    main = ws / "src" / "main.py"
    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps")
    cp = cpm.create(paths=[main])
    real = shutil.copy2

    def guarded(src, dst, *a, **k):
        if Path(dst) == main:
            raise PermissionError("in use")
        return real(src, dst, *a, **k)
    monkeypatch.setattr("tadamoo.checkpoints.shutil.copy2", guarded)
    assert cpm.restore(cp) is True                                                       # nothing needed copying


def test_the_program_a_command_runs_is_not_kept_but_its_arguments_are(ws):
    script = put(ws / "run.py")
    program = put(ws / "tool.exe")
    names = {p.name for p in shell_paths(ws, f'"{program}" run.py && "{program}" > made.txt')}
    assert names == {"run.py", "made.txt"} and script.exists()
