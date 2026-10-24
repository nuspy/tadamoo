import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.audit import AuditLedger
from harness.rewind import checkpoint_report, main, rewind
from harness.stack import AgentConfig, build_agent
from test_agent_loop import ScriptedClient, call, say


def run_agent(ws, data, steps, **kw):
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(data), use_memory=False, permission_mode="auto", **kw), client=ScriptedClient(*steps))
    built.agent.run("go")
    built.close()


@pytest.fixture
def work(tmp_path):
    ws, data = tmp_path / "ws", tmp_path / "data"
    ws.mkdir()
    (ws / "a.txt").write_text("one\n", encoding="utf-8")
    return ws, data


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def test_the_checkpoints_are_listed_with_what_ran_after_each(work):
    ws, data = work
    run_agent(ws, data, [call("write_file", {"path": "a.txt", "content": "two\n"}),
                         call("shell_command", {"command": "python -c \"print(1)\""}, "c2"), say("done")])
    cp, later = checkpoint_report(data, ws)                                   # one checkpoint for each turn that changes something
    assert cp["description"] == "before write_file" and cp["files"] == 1 and later["description"] == "before shell_command"
    assert [(a["tool"], a["effect"]) for a in cp["after"]] == [("write_file", "reversible"), ("shell_command", "irreversible")]
    assert [a["tool"] for a in cp["not_undone"]] == ["shell_command"] and "python -c" in cp["not_undone"][0]["what"]
    assert [a["tool"] for a in later["after"]] == ["shell_command"]


def test_going_back_restores_files_and_removes_new_ones_and_says_how_to_undo_it(work):
    ws, data = work
    run_agent(ws, data, [call("write_file", {"path": "a.txt", "content": "two\n"}), call("write_file", {"path": "new.txt", "content": "x"}, "c2"), say("ok")])
    cp = checkpoint_report(data, ws)[0]
    code, out, _ = cli("--workspace", str(ws), "--data-dir", str(data), "--to", cp["id"])
    assert code == 0 and "To undo this rewind" in out
    assert (ws / "a.txt").read_text(encoding="utf-8") == "one\n" and not (ws / "new.txt").exists()
    safety = out.split("--to ")[-1].strip()
    assert cli("--workspace", str(ws), "--data-dir", str(data), "--to", safety)[0] == 0           # the rewind itself is undone
    assert (ws / "a.txt").read_text(encoding="utf-8") == "two\n" and (ws / "new.txt").exists()


def test_a_rewind_over_actions_that_stay_done_asks_for_yes(work):
    ws, data = work
    run_agent(ws, data, [call("write_file", {"path": "a.txt", "content": "two\n"}),
                         call("shell_command", {"command": "python -c \"print(1)\""}, "c2"), say("done")])
    cp = checkpoint_report(data, ws)[0]
    code, _, err = cli("--workspace", str(ws), "--data-dir", str(data), "--to", cp["id"])
    assert code == 3 and "stay done" in err and "python -c" in err
    assert (ws / "a.txt").read_text(encoding="utf-8") == "two\n"                                    # nothing was touched
    assert cli("--workspace", str(ws), "--data-dir", str(data), "--to", cp["id"], "--yes")[0] == 0
    assert (ws / "a.txt").read_text(encoding="utf-8") == "one\n"


def test_keep_new_leaves_files_created_since(work):
    ws, data = work
    run_agent(ws, data, [call("write_file", {"path": "a.txt", "content": "two\n"}), call("write_file", {"path": "new.txt", "content": "x"}, "c2"), say("ok")])
    cp = checkpoint_report(data, ws)[0]
    assert cli("--workspace", str(ws), "--data-dir", str(data), "--to", cp["id"], "--keep-new")[0] == 0
    assert (ws / "new.txt").exists() and (ws / "a.txt").read_text(encoding="utf-8") == "one\n"


def test_a_rewind_is_written_in_the_audit_log_and_the_chain_still_verifies(work):
    ws, data = work
    run_agent(ws, data, [call("write_file", {"path": "a.txt", "content": "two\n"}), say("ok")])
    cp = checkpoint_report(data, ws)[0]
    result = rewind(data, ws, cp["id"])
    ledger = AuditLedger(data / "audit.jsonl")
    last = ledger.records[-1]
    assert (last["actor"], last["action"], last["details"]["to"]) == ("user", "rewind", cp["id"]) and last["details"]["safety_checkpoint"] == result["safety_checkpoint"]
    assert ledger.verify_integrity()[0]


def test_listing_in_text_and_json_and_the_errors(work):
    ws, data = work
    assert "No checkpoints" in cli("--workspace", str(ws), "--data-dir", str(data))[1]
    run_agent(ws, data, [call("shell_command", {"command": "python -c \"print(1)\""}), say("ok")])
    code, out, _ = cli("--workspace", str(ws), "--data-dir", str(data))
    assert code == 0 and "before shell_command" in out and "cannot be undone" in out
    assert json.loads(cli("--workspace", str(ws), "--data-dir", str(data), "--json")[1])[0]["not_undone"]
    assert cli("--workspace", str(ws), "--data-dir", str(data), "--to", "cp_9_9")[0] == 2
    assert cli("--workspace", str(ws / "missing"))[0] == 2
    cp = checkpoint_report(data, ws)[0]["id"]
    assert cli("--workspace", str(ws), "--data-dir", str(data), "--to", "../etc", "--yes")[0] == 2
    assert cli("--workspace", str(ws), "--data-dir", str(data), "--to", cp, "--yes", "--json")[0] == 0
