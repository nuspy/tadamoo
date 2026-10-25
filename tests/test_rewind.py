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


# -- the classes of effect, read before a conversation or a plan is rewound ---------------------------------------------------


def _call(cid, name, **args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_a_conversation_rewind_lists_what_stays_done_and_what_the_working_copy_can_put_back(tmp_path):
    from harness.rewind import session_impact
    outside = str(tmp_path / "elsewhere" / "x.txt")
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_call("1", "read_file", path="a"), _call("2", "write_file", path="b.txt"), _call("3", "shell_command", command="make"),
                                                              _call("4", "phone_call", number="+39 1"), _call("5", "request_payment", amount="5"), _call("6", "write_file", path=outside),
                                                              _call("7", "some_server__send_mail", to="x")]},
        {"role": "tool", "tool_call_id": "3", "name": "shell_command", "content": "built"},
    ]
    found = session_impact(messages, workspace=str(tmp_path / "ws"))
    assert [a["what"] for a in found["reversible"]] == ["write_file b.txt"]
    assert [(a["tool"], a["effect"]) for a in found["not_undone"]] == [("shell_command", "irreversible"), ("phone_call", "irreversible"), ("request_payment", "irreversible"),
                                                                       ("write_file", "irreversible"), ("some_server__send_mail", "unknown")]
    refused = [{"role": "assistant", "content": "", "tool_calls": [_call("1", "shell_command", command="rm x")]},
               {"role": "tool", "tool_call_id": "1", "name": "shell_command", "content": "Permission denied: no."}]
    assert session_impact(refused) == {"reversible": [], "not_undone": []}
    assert session_impact([{"role": "assistant", "content": "", "tool_calls": [_call("1", "newtool")]}], learned={"newtool": "reversible"})["reversible"]


def _plan():
    from harness.planner.dag import Assignee, NodeType, PlanDAG, PlanNode
    dag = PlanDAG(plan_id="p1", title="Trip", target_type="occasional")
    for nid, deps in (("book", []), ("call", ["book"]), ("pay", ["call"])):
        dag.add_node(PlanNode(nid, NodeType.ACTION, nid.title(), "", Assignee.AGENT, dependencies=list(deps)))
    return dag


def test_a_plan_rewind_names_the_steps_whose_actions_stay_done_and_waits_for_a_yes():
    from harness.planner.dag import PlanError
    from harness.rewind import note_plan_effect, plan_impact
    dag = _plan()
    for nid in ("book", "call", "pay"):
        dag.mark(nid, "completed")
    note_plan_effect(dag, "book", "write_file", "reversible", "write_file trip.md")
    note_plan_effect(dag, "call", "phone_call", "irreversible", "phone_call +39 1")
    note_plan_effect(dag, "pay", "request_payment", "irreversible", "request_payment 20")
    note_plan_effect(dag, "pay", "read_file", "pure", "never kept")
    impact = plan_impact(dag, "call")
    assert impact["steps"] == ["call", "pay"] and impact["ran"] == ["call", "pay"] and impact["reversible"] == []
    assert [(e["step"], e["tool"]) for e in impact["not_undone"]] == [("call", "phone_call"), ("pay", "request_payment")]
    assert [e["step"] for e in plan_impact(dag, "book")["reversible"]] == ["book"]
    with pytest.raises(PlanError, match="confirm"):
        dag.rewind("call")
    assert dag.nodes["call"].status == "completed" and dag.nodes["pay"].status == "completed"            # nothing changed
    assert dag.rewind("call", accept_irreversible=True) == ["call", "pay"]
    kept = dag.nodes["call"].state_variables["history"][-1]
    assert kept["status"] == "completed" and [e["tool"] for e in kept["still_done"]] == ["phone_call"]          # listed as still done
    assert "effects" not in dag.nodes["call"].state_variables and dag.nodes["book"].status == "completed"


def test_a_plan_without_recorded_actions_or_with_only_files_rewinds_as_before():
    from harness.rewind import note_plan_effect
    dag = _plan()
    dag.mark("book", "completed")
    note_plan_effect(dag, "book", "write_file", "reversible", "write_file trip.md")
    assert dag.rewind("book") == ["book", "call", "pay"]
    assert dag.nodes["book"].state_variables["history"][-1]["effects"][0]["tool"] == "write_file"


def test_the_plan_events_note_an_effect_preview_the_rewind_and_ask_for_the_yes():
    from harness.planner.bridge import PlanCanvasBridge
    bridge = PlanCanvasBridge(_plan())
    for nid in ("book", "call", "pay"):
        assert bridge.handle({"type": "plan:mark", "node_id": nid, "status": "completed"})["ok"]
    assert bridge.handle({"type": "plan:note_effect", "node_id": "call", "tool": "phone_call", "effect": "irreversible", "what": "phone_call +39 1", "actor": "system"})["ok"]
    assert bridge.handle({"type": "plan:note_effect", "node_id": "call", "tool": "read_file", "effect": "pure"})["effect"] == {}
    preview = bridge.handle({"type": "plan:rewind_impact", "node_id": "book"})
    assert preview["ok"] and [e["tool"] for e in preview["not_undone"]] == ["phone_call"]
    refused = bridge.handle({"type": "plan:rewind", "node_id": "book"})
    assert not refused["ok"] and "phone_call +39 1" in refused["error"] and bridge.dag.nodes["pay"].status == "completed"
    done = bridge.handle({"type": "plan:rewind", "node_id": "book", "accept_irreversible": True})
    assert done["ok"] and done["rewound"] == ["book", "call", "pay"] and [e["tool"] for e in done["not_undone"]] == ["phone_call"]
    assert not bridge.handle({"type": "plan:note_effect", "node_id": "ghost", "tool": "x", "effect": "irreversible"})["ok"]
