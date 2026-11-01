"""Divergent tasks and component usage in the evaluation reports (harness/eval/traces.py). CPU only, no model."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.eval import EvalRunner, generate_eval_report, load_task_set
from harness.eval.cases.agentrun import CountingClient
from harness.eval.cases.core import Arm, CaseTrial, Context, Experiment, Rule, render_report
from harness.eval.runner import EvalResult, Trial
from harness.eval.traces import call_trace, component_usage, divergence, divergent_section, divergent_tasks, first_difference, observe
from test_agent_loop import ScriptedClient, call, say


def c(name, **args):
    return {"name": name, "args": str(args), "key": name + str(sorted(args.items()))}


def run_call(name, args=None, decision="executed", feedback="", written=None):
    return SimpleNamespace(tool_name=name, arguments=args or {}, written=written, decision=decision, feedback=feedback)


def turn(*calls):
    return SimpleNamespace(tool_calls=list(calls))


# -- where trials part ways ---------------------------------------------------------------------------------------

def test_first_difference_finds_the_call_or_the_end_of_the_shorter_trace():
    a = [c("read_file", path="x"), c("edit_file", path="x"), c("shell_command", command="pytest")]
    assert first_difference(a, a) is None
    assert first_difference(a, [a[0], c("edit_file", path="y"), a[2]]) == 1          # same tool, other arguments
    assert first_difference(a, a[:2]) == 2                                          # one ends sooner
    assert first_difference([], a) == 0


def test_a_task_that_always_or_never_passes_has_no_divergence():
    t = [c("a")]
    assert divergence([(1, True, t), (2, True, t)]) is None
    assert divergence([(1, False, t), (2, False, [c("b")])]) is None


def test_the_divergence_is_the_earliest_call_where_a_passing_and_a_failing_trial_differ():
    ok = [c("read_file", path="x"), c("edit_file", path="x"), c("shell_command", command="pytest")]
    late = [ok[0], ok[1], c("shell_command", command="ls")]
    early = [c("search_code", query="x"), ok[1]]
    d = divergence([(1, True, ok), (2, False, late), (3, False, early)])
    assert d["index"] == 0 and (d["passed_rep"], d["failed_rep"]) == (1, 3)
    assert d["passed_call"]["name"] == "read_file" and d["failed_call"]["name"] == "search_code"
    d = divergence([(1, True, ok), (2, False, late)])
    assert d["index"] == 2 and d["failed_call"]["args"] == str({"command": "ls"})
    d = divergence([(1, True, ok), (2, False, ok[:1])])
    assert d["index"] == 1 and d["failed_call"] is None


def test_without_any_trace_the_task_is_still_listed_with_no_index():
    rows = divergent_tasks({"t": [(1, True, []), (2, False, [])], "u": [(1, True, [])]})
    assert [(r["task"], r["passed"], r["of"], r["index"]) for r in rows] == [("t", 1, 2, None)]
    assert "no trace recorded" in "\n".join(divergent_section(rows))


def test_the_section_is_markdown_and_empty_when_nothing_diverges():
    assert divergent_section([]) == []
    rows = divergent_tasks({"fix": [(1, True, [c("read_file", path="a|b")]), (2, False, [c("write_file", path="a")])]})
    text = "\n".join(divergent_section(rows))
    assert "## Divergent tasks" in text and "| `fix` | 1/2 | 0 |" in text and "`write_file`" in text and "a|b" not in text      # a bar in an argument cannot split the table


# -- components ---------------------------------------------------------------------------------------------------

def test_the_trace_is_the_calls_as_the_model_wrote_them():
    turns = [turn(run_call("add", {"a": 17}, written={"a": "17"})), turn(), turn(run_call("x", {"k": "v" * 300}))]
    trace = call_trace(turns)
    assert [t["name"] for t in trace] == ["add", "x"] and '"17"' in trace[0]["args"]
    assert trace[1]["args"].endswith("...") and len(trace[1]["args"]) == 103
    assert call_trace([turn(run_call("x", {"k": "a" * 300}))])[0]["key"] != call_trace([turn(run_call("x", {"k": "a" * 299 + "b"}))])[0]["key"]


def test_components_are_counted_from_what_the_run_recorded():
    turns = [turn(run_call("load_skill"), run_call("spawn_subagent"), run_call("recall"), run_call("recall", decision="denied")),
             turn(run_call("search_past"), run_call("shell_command", decision="blocked"), run_call("read_file", feedback="lint: 2 errors"), run_call("ask_council"))]
    audit = [{"action": "stop_denied"}, {"action": "compaction"}, {"action": "stop_denied"}, {"action": "course_check"}]
    used = component_usage(turns, audit, compactions=3, side_uses={"triage": 2, "compaction": 1})
    assert used == {"skills": 1, "subagents": 2, "memory_recalls": 2, "hook_blocks": 1, "hook_feedback": 1, "stop_denials": 2, "course_checks": 1, "compactions": 3,
                    "side_uses": 3, "side:compaction": 1, "side:triage": 2}
    assert component_usage([]) == {k: 0 for k in ("skills", "subagents", "memory_recalls", "hook_blocks", "hook_feedback", "stop_denials", "course_checks", "compactions", "side_uses")}


def test_observe_reads_the_counters_of_the_agent_the_ledger_and_the_client():
    agent = SimpleNamespace(context=SimpleNamespace(compaction=SimpleNamespace(count=2)))
    client = SimpleNamespace(side_uses={"judge": 4})
    trace, used = observe([turn(run_call("load_skill"))], agent, SimpleNamespace(records=[{"action": "stop_denied"}]), client)
    assert trace[0]["name"] == "load_skill" and (used["skills"], used["compactions"], used["stop_denials"], used["side:judge"]) == (1, 2, 1, 4)
    assert observe([], None, None, None)[1]["compactions"] == 0                  # a loop without a context, ledger or counting client


def test_the_counting_client_counts_technical_session_calls_by_use():
    inner = SimpleNamespace(chat_completion=lambda *a, **kw: {"usage": {}})
    client = CountingClient(inner)
    client.chat_completion(messages=[], extra={"cache_slot": "side", "side_use": "triage"})
    client.chat_completion(messages=[], extra={"side_use": "triage"})
    client.chat_completion(messages=[])
    assert client.side_uses == {"triage": 2} and client.calls == 3


# -- the runner and the reports -------------------------------------------------------------------------------------

def test_a_trial_records_its_trace_and_components():
    _, tasks = load_task_set("core")
    task = next(t for t in tasks if t.id == "read_and_answer")
    steps = [call("read_file", {"path": "data/config.json"}), say("port 7311")]
    res = EvalRunner(client_factory=lambda t, r: ScriptedClient(*steps)).evaluate_task(task, 1)
    trial = res.trials[0]
    assert [t["name"] for t in trial.trace] == ["read_file"] and "data/config.json" in trial.trace[0]["args"]
    assert trial.components["skills"] == 0 and trial.components["compactions"] == 0


def test_the_eval_report_lists_divergent_tasks_and_components():
    def trial(rep, passed, calls, **used):
        return Trial(repetition=rep, passed=passed, trace=[c(n) for n in calls], components=used)
    mixed = EvalResult("fix_bug", "coding", 2, 1.0, False, [trial(1, True, ["read_file", "edit_file"], skills=1), trial(2, False, ["read_file", "shell_command"], skills=2, compactions=1)])
    steady = EvalResult("count", "reading", 2, 1.0, True, [trial(1, True, ["tree"]), trial(2, True, ["tree"])])
    text = generate_eval_report([mixed, steady])
    assert "## Divergent tasks" in text and "| `fix_bug` | 1/2 | 1 | #1: `edit_file`" in text and "`count`" not in text.split("## Divergent tasks")[1]
    assert "## Components used" in text and "| `all trials` | 4 | 3 (0.75) | 1 (0.25) |" in text
    assert "## Divergent tasks" not in generate_eval_report([steady]) and "## Components used" not in generate_eval_report([steady])


def test_the_case_report_lists_divergent_tasks_per_arm_and_components_per_arm():
    arms = [Arm("A", "a"), Arm("B", "b")]
    exp = Experiment(id="ex", title="t", question="q", arms=arms, rule=Rule("success", "higher", 0.1, "x", challengers=("B",)), columns=("success",),
                     tasks=lambda: [], run_trial=lambda *a: None)
    ts = [CaseTrial("A", "t1", 1, True, trace=[c("read_file")], components={"skills": 1}), CaseTrial("A", "t1", 2, False, trace=[c("tree")], components={"skills": 2}),
          CaseTrial("B", "t1", 1, True, trace=[c("tree")]), CaseTrial("B", "t1", 2, True, trace=[c("tree")]),
          CaseTrial("B", "t2", 1, False, invalid=True, trace=[c("x")])]
    text = render_report(exp, Context(plumbing=True), ts, 2)
    section = text.split("## Divergent tasks")[1].split("## Components used")[0]
    assert "| `A` | `t1` | 1/2 | 0 |" in section and "`B`" not in section
    assert "| `A` | 2 | 3 (1.50) |" in text and "| `B` | 2 |" in text                   # the invalid trial is left out of B
