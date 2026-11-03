"""The case studies run end to end against scripted models and a scripted server: no GPU, no real model. They show that each experiment runs, scores a
good run and a bad one differently, and that its arms reach the settings they name."""
import copy
import json
import random
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo import optimize as opt
from tadamoo import questions as qs
from tadamoo.eval import cases
from tadamoo.eval.cases import e1_target, e2_gate, e3_errors, e4_intake, e5_compaction, e6_longform, e7_cache, e8_optimize, e9_context
from tadamoo.eval.cases.agentrun import FIXED_NOW
from tadamoo.eval.cases.core import Context, decide, render_report, run_experiment
from tadamoo.eval.cases.fixtures import context_probes, gate_tasks, long_turn_repo, shop_repo
from test_cases_core import perfect
from test_intake import form

USAGE = {"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 40}}


@pytest.fixture(autouse=True)
def _no_clock(monkeypatch):
    monkeypatch.setattr("tadamoo.stack.local_now", None)             # these tests read the messages; the clock note has its own tests


class Fake:
    """A model that answers by a function of the request, and keeps every request."""

    def __init__(self, fn):
        self.fn, self.requests = fn, []

    def chat_completion(self, messages, tools=None, **kw):
        self.requests.append({"messages": copy.deepcopy(messages), "tools": tools, **kw})
        reply = self.fn(messages, tools, kw)
        reply.setdefault("usage", USAGE)
        return reply


def say(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def tool(name, args, i=1):
    return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}, "finish_reason": "tool_calls"}]}


def json_reply(obj):
    return say(json.dumps(obj))


def writes(existing, files, start=0):
    """Tool calls that write `files`; a file that already exists is read first, as the harness requires."""
    out, i = [], start
    for path, text in files:
        if path in existing:
            out.append(tool("read_file", {"path": path}, i))
            i += 1
        out.append(tool("write_file", {"path": path, "content": text}, i))
        i += 1
    return out


class Steps:
    """The next canned reply each time, whatever the request."""

    def __init__(self, *steps):
        self.steps = list(steps)

    def __call__(self, messages, tools, kw):
        return self.steps.pop(0)


# -- E2 ------------------------------------------------------------------------------------------------------------------

def trap_steps():
    raw = next(t for t in e2_gate.tasks() if t["id"] == "t2_trap_shared_pad")
    ex = raw["files"]
    naive = "def pad(text, width):\n    return str(text).rjust(width)\n"
    return raw, [*writes(ex, [("textfmt.py", naive)]), say("Fixed."),
                 *writes(ex, [("textfmt.py", ex["textfmt.py"]), ("report.py", raw["solution"]["report.py"])], 10), say("Fixed properly.")]


def test_e2_without_the_gate_the_naive_fix_ends_finished_with_a_failing_test_and_with_it_the_model_is_sent_back():
    raw, steps = trap_steps()
    arms = {a.name: a for a in e2_gate.ARMS}
    off = e2_gate.run_one(arms["A"], raw, 1, Context(client=Fake(Steps(*copy.deepcopy(steps))), power_limit_w=400.0))
    assert not off.success and off.metrics["finished_failing"] == 1.0 and off.metrics["passes"] == 0 and "unittest" in " ".join(off.notes) and not off.invalid
    on_client = Fake(Steps(*copy.deepcopy(steps)))
    on = e2_gate.run_one(arms["B"], raw, 1, Context(client=on_client, power_limit_w=400.0))
    assert on.success and on.metrics["finished_failing"] == 0.0 and on.metrics["passes"] == 1
    assert any("The project's checks failed" in str(m.get("content")) for r in on_client.requests for m in r["messages"]), "the model was told what failed"
    assert on.metrics["prompt_tokens"] == 100 * len(on_client.requests) and on.metrics["cached_tokens"] == 40 * len(on_client.requests)


def test_e2_a_clean_fix_passes_in_both_arms_and_the_experiment_reports():
    raw = next(t for t in e2_gate.tasks() if t["id"] == "t1_window_off_by_one")
    exp = e2_gate.experiment()
    exp.tasks = lambda: [raw]

    def model(messages, tools, kw):
        n = sum(1 for m in messages if m["role"] == "tool")
        if n == 0:
            return tool("read_file", {"path": "seq.py"})
        if n == 1:
            return tool("write_file", {"path": "seq.py", "content": raw["solution"]["seq.py"]}, 2)
        return say("Fixed it, and the tests pass.")
    done = run_experiment(exp, Context(client=Fake(model), power_limit_w=400.0), 2)
    assert len(done) == 6 and all(t.success for t in done), [t.notes for t in done]
    text = render_report(exp, Context(power_limit_w=400.0), done, 2)
    assert "400 W" in text and "verify-gate" in text and "no difference beyond the noise" in text


# -- E1 ------------------------------------------------------------------------------------------------------------------

def target_model(goal, seen):
    pending = list(goal["solution"].items())
    existing = {**shop_repo.REPO, **goal["files"]}
    todo = []                                                   # tool calls still to make

    def refill():
        if pending and not todo:
            path, text = pending.pop(0)
            todo.extend(writes(existing, [(path, text)], len(pending) * 3))

    def fn(messages, tools, kw):
        rf = kw.get("response_format")
        if rf:
            name = rf["json_schema"]["name"]
            if name == "target_plan":
                return json_reply({"criteria": [{"id": "c1", "text": "The goal is done", "source": ""}],
                                   "tasks": [{"id": "x", "title": "Do the goal", "prompt": "Do everything the target asks.", "serves": ["c1"], "depends_on": [], "subtasks": []}]})
            if name == "target_verdict":
                return json_reply({"verdicts": [{"id": "c1", "status": "achieved", "evidence": "the tests passed, shown in the check output", "evidence_refs": ["E1"], "missing": ""}]})
            raise AssertionError(name)
        last = messages[-1]
        if last["role"] == "user":
            seen.append(last["content"])
        if last["role"] == "tool":
            refill()
            return todo.pop(0) if todo else say("Done, and the tests pass.")
        if last["content"].startswith("Target Loop · check"):
            return tool("shell_command", {"command": last["content"].split("\n\n")[-1].strip()})
        if "Target Loop · task" in last["content"]:
            refill()
            if todo:
                return todo.pop(0)
        return say("Understood.")
    return fn


@pytest.mark.parametrize("arm, first_is_alone", [("A", False), ("B", True), ("A~aa", False)])
def test_e1_the_arm_decides_whether_the_text_goes_alone_in_the_first_message(arm, first_is_alone):
    goal = next(g for g in e1_target.goals() if g["id"] == "g3_two_failing_tests")
    seen = []
    trial = e1_target.run_goal(next(a for a in e1_target.ARMS if a.name == arm), goal, 1, Context(client=Fake(target_model(goal, seen)), power_limit_w=400.0))
    assert trial.success and not trial.invalid and trial.metrics["passes"] == 1 and trial.metrics["criteria_met"] == 1.0
    firsts = [m for m in seen if not m.startswith("Target Loop · check")]
    if first_is_alone:
        assert firsts[0] == goal["text"] and firsts[1].startswith("Target Loop · task 1 of 1") and "Target:" in firsts[1]
    else:
        assert firsts[0].startswith(goal["text"]) and "Target Loop · task 1 of 1" in firsts[0] and firsts[0] != goal["text"] and len(firsts) == 1
    assert trial.metrics["prompt_tokens"] > 0 and trial.metrics["seconds"] > 0


def test_e1_a_goal_the_agent_does_not_do_is_a_failed_trial_with_the_failed_checks_named():
    goal = next(g for g in e1_target.goals() if g["id"] == "g1_clamp")

    def lazy(messages, tools, kw):
        if kw.get("response_format"):
            return target_model(goal, [])(messages, tools, kw)
        last = messages[-1]
        if last["role"] == "user" and last["content"].startswith("Target Loop · check"):
            return tool("shell_command", {"command": last["content"].split("\n\n")[-1].strip()})
        return say("All done.")
    trial = e1_target.run_goal(e1_target.ARMS[0], goal, 1, Context(client=Fake(lazy), power_limit_w=400.0))
    assert not trial.success and 0 < trial.metrics["criteria_met"] < 1 and any("clamp" in n for n in trial.notes)


# -- E3 (a scripted server, the real session and client) --------------------------------------------------------------------------

def test_e3_a_request_the_server_always_refuses_never_ends_without_the_classifier_and_ends_visibly_with_it():
    ctx = Context(plumbing=True, extra={"deadline_s": 0.8})
    scenario = {s["id"]: s for s in e3_errors.tasks()}
    a, b = e3_errors.ARMS[0], e3_errors.ARMS[1]
    off = e3_errors.run_scenario(a, scenario["deterministic_401"], 1, ctx)
    on = e3_errors.run_scenario(b, scenario["deterministic_401"], 1, ctx)
    assert not off.success and off.metrics["never_ends"] == 1.0 and off.metrics["retries"] >= 3
    assert on.success and on.metrics["never_ends"] == 0.0 and on.metrics["retries"] == 0 and on.metrics["visible_s"] < 0.8


def test_e3_a_transient_burst_and_a_model_that_is_loading_are_waited_for_in_both_arms():
    ctx = Context(plumbing=True, extra={"deadline_s": 3.0})
    scenario = {s["id"]: s for s in e3_errors.tasks()}
    for arm in e3_errors.ARMS[:2]:
        for sid, retries in (("transient_429_500", 2), ("transient_timeout", 1), ("loading_400", 1)):
            t = e3_errors.run_scenario(arm, scenario[sid], 1, ctx)
            assert t.success and t.metrics["retries"] == retries and t.metrics["wrong_end"] == 0.0 and t.metrics["never_ends"] == 0.0, (arm.name, sid, t.notes)


def test_e3_overflow_is_still_retried_with_the_classifier_and_the_experiment_reports_it_as_plumbing():
    exp = e3_errors.experiment()
    exp.tasks = lambda: [s for s in e3_errors.tasks() if s["id"] in ("deterministic_400", "overflow_forever", "transient_mixed")]
    done = run_experiment(exp, Context(plumbing=True, extra={"deadline_s": 1.5}), 1)
    by = {(t.arm, t.task): t for t in done}
    assert by[("B", "overflow_forever")].metrics["never_ends"] == 1.0 and by[("B", "deterministic_400")].metrics["never_ends"] == 0.0
    assert by[("A", "transient_mixed")].success and by[("B", "transient_mixed")].success
    (verdict,) = decide({k: [t for t in done if t.arm == k] for k in ("A", "A~aa", "B")}, exp.rule)
    assert verdict.primary == "never_ends" and verdict.effect > 0
    assert "Plumbing check" in render_report(exp, Context(plumbing=True), done, 1)


def test_the_scripted_server_serves_text_errors_and_streams():
    import urllib.error
    import urllib.request

    from tadamoo.client import TadamooClient, TadamooError
    from tadamoo.eval.cases.scripted_server import ScriptedServer
    with ScriptedServer([{"error": 429, "message": "slow down"}, {"ok": "hello there"}, {"ok": "streamed text"}]) as srv:
        client = TadamooClient(srv.url)
        with pytest.raises(TadamooError) as e:
            client.chat_completion([{"role": "user", "content": "hi"}])
        assert e.value.status == 429 and "slow down" in str(e.value)
        assert client.chat_completion([{"role": "user", "content": "hi"}])["choices"][0]["message"]["content"] == "hello there"
        pieces = []
        out = client.chat_completion([{"role": "user", "content": "hi"}], on_delta=lambda k, t: pieces.append(t))
        assert "".join(pieces) == "streamed text" and out["usage"]["prompt_tokens"] == 50
        with pytest.raises(TadamooError) as spent:
            client.chat_completion([{"role": "user", "content": "hi"}])
        assert spent.value.status == 500 and srv.count() == 4


# -- E4 ------------------------------------------------------------------------------------------------------------------

def intake_model(row, forms):
    def fn(messages, tools, kw):
        if kw.get("response_format"):
            return json_reply(forms)
        everything = json.dumps(messages)
        last = messages[-1]
        if last["role"] == "tool":
            return say("Done.")
        if row["id"] == "date1":
            m = re.search(r'tomorrow\\" = \w+ (\d{4}-\d\d-\d\d)', everything)
            return tool("write_file", {"path": "reminder.txt", "content": f"Dentist: {m.group(1) if m else '2026-10-30'}\n"})
        if row["id"] == "ask1" and last["role"] == "user" and last["content"].startswith(qs.ANSWER_HEADER):
            return tool("write_file", {"path": "summary_acme.md", "content": "Summary for Acme\n"})
        return say("Ok, I have started.")
    return fn


def test_e4_the_rules_arm_gives_the_model_the_date_and_the_off_arm_does_not():
    row = next(r for r in e4_intake.prompts() if r["id"] == "date1")
    arms = {a.name: a for a in e4_intake.ARMS}
    off = e4_intake.run_prompt(arms["A"], row, 1, Context(client=Fake(intake_model(row, None)), power_limit_w=400.0))
    rules = e4_intake.run_prompt(arms["B"], row, 1, Context(client=Fake(intake_model(row, None)), power_limit_w=400.0))
    assert not off.success and off.metrics["date_ok"] == 0.0 and off.metrics["good"] == 0.0
    assert rules.success and rules.metrics["date_ok"] == 1.0 and rules.metrics["good"] == 1.0 and rules.metrics["ask_correct"] == 1.0 and rules.metrics["over_ask"] == 0.0
    assert rules.metrics["model_calls"] == off.metrics["model_calls"], "the rules arm adds no model call"


def test_e4_the_full_arm_asks_when_the_form_says_a_wrong_guess_is_costly_and_the_reply_finishes_the_task():
    row = dict(next(r for r in e4_intake.prompts() if r["id"] == "amb2"), id="ask1",
               text="Write the summary file for the client, and then send it to the team by email when you have finished, please. " * 2)
    triage = form(size="small", subtasks=[], stated_constraints=[], evidence=[], ambiguity="material", missing_info=[
        {"what": "Which client", "why": "two are listed", "default": "Acme", "findable": "no", "wrong_guess_cost": "high", "reversible": False, "owner": "user"}])
    arms = {a.name: a for a in e4_intake.ARMS}
    full = e4_intake.run_prompt(arms["C"], row, 1, Context(client=Fake(intake_model(row, triage)), power_limit_w=400.0))
    assert full.metrics["ask_correct"] == 1.0 and full.metrics["missed_ask"] == 0.0 and full.success and full.metrics["good"] == 1.0
    off = e4_intake.run_prompt(arms["A"], row, 1, Context(client=Fake(intake_model(row, triage)), power_limit_w=400.0))
    assert off.metrics["missed_ask"] == 1.0 and off.metrics["good"] == 0.0 and not off.success
    assert full.metrics["model_calls"] > off.metrics["model_calls"] - 1                     # the triage call is counted


def test_e4_score_row_marks_asking_when_not_needed_and_not_asking_when_needed():
    plain = next(r for r in e4_intake.prompts() if r["id"] == "plain1")
    risky = next(r for r in e4_intake.prompts() if r["id"] == "risk1")
    assert e4_intake.score_row(plain, False, []) == {"ask_correct": 1.0, "over_ask": 0.0, "missed_ask": 0.0, "good": 1.0}
    assert e4_intake.score_row(plain, True, [])["over_ask"] == 1.0 and e4_intake.score_row(plain, True, [])["good"] == 0.0
    assert e4_intake.score_row(risky, False, [])["missed_ask"] == 1.0
    assert e4_intake.score_row(risky, True, ["x"])["good"] == 0.0 and e4_intake.score_row(risky, True, [])["good"] == 1.0
    dated = next(r for r in e4_intake.prompts() if r["id"] == "date1")
    assert e4_intake.score_row(dated, False, ["wrong date"])["date_ok"] == 0.0 and e4_intake.score_row(dated, False, [])["date_ok"] == 1.0


# -- E8 ------------------------------------------------------------------------------------------------------------------

class WithSession(Fake):
    """A model that answers by a function of the request and reports a technical session (or none)."""

    def __init__(self, fn, side=30000):
        super().__init__(fn)
        self.side = side

    def side_context(self):
        return self.side


def optimize_model(rewrite):
    """The helper pass answers `rewrite`; the conversation's model writes port.txt only when it was told to create the file, as the rewrite says."""
    def fn(messages, tools, kw):
        if messages[0]["content"] == opt.SYSTEM_PROMPT:
            return say(rewrite)
        if messages[-1]["role"] == "tool":
            return say("Done.")
        if "Create the file port.txt" in json.dumps(messages):
            return tool("write_file", {"path": "port.txt", "content": "8080\n"})
        return say("Ok, I have started.")
    return fn


def test_e8_the_on_arm_gives_the_model_the_rewrite_and_the_off_arm_does_not():
    row = next(r for r in e8_optimize.prompts() if r["id"] == "num1")
    arms = {a.name: a for a in e8_optimize.ARMS}
    rewrite = "Create the file port.txt and write the number 8080 in it, with nothing else in the file."
    off_client, on_client = WithSession(optimize_model(rewrite)), WithSession(optimize_model(rewrite))
    off = e8_optimize.run_prompt(arms["A"], row, 1, Context(client=off_client, power_limit_w=400.0))
    on = e8_optimize.run_prompt(arms["B"], row, 1, Context(client=on_client, power_limit_w=400.0))
    assert not off.success and off.metrics["good"] == 0.0 and off.metrics["optimized"] == 0.0 and off.metrics["model_calls"] == 1
    assert on.success and on.metrics["good"] == 1.0 and on.metrics["optimized"] == 1.0 and on.metrics["protected_kept"] == 1.0
    assert on.metrics["model_calls"] == off.metrics["model_calls"] + 2, "the helper call and the write are counted"
    assert "optimize_seconds" in on.metrics and off.metrics["optimize_seconds"] == 0.0
    assert [r["extra"].get("cache_slot") for r in on_client.requests if r["messages"][0]["content"] == opt.SYSTEM_PROMPT] == ["side"]


def test_e8_a_rewrite_that_drops_the_number_is_thrown_away_so_the_model_still_reads_what_the_person_wrote():
    row = next(r for r in e8_optimize.prompts() if r["id"] == "num1")
    arms = {a.name: a for a in e8_optimize.ARMS}
    on = e8_optimize.run_prompt(arms["B"], row, 1, Context(client=WithSession(optimize_model("Create the file port.txt and write the port number in it.")), power_limit_w=400.0))
    assert on.metrics["rejected"] == 1.0 and on.metrics["lost"] == 1.0 and on.metrics["optimized"] == 0.0 and on.metrics["protected_kept"] == 1.0
    assert not on.success and any("number: 8080" in n for n in on.notes)


def test_e8_without_a_technical_session_the_on_arm_is_invalid_not_a_failure():
    row = next(r for r in e8_optimize.prompts() if r["id"] == "num1")
    arms = {a.name: a for a in e8_optimize.ARMS}
    on = e8_optimize.run_prompt(arms["B"], row, 1, Context(client=WithSession(optimize_model("x"), side=0), power_limit_w=400.0))
    assert on.invalid and "technical session" in on.error
    off = e8_optimize.run_prompt(arms["A"], row, 1, Context(client=WithSession(optimize_model("x"), side=0), power_limit_w=400.0))
    assert not off.invalid


def test_e8_runs_end_to_end_and_reports_the_rule():
    rows = [r for r in e8_optimize.prompts() if r["id"] == "num1"]
    exp = e8_optimize.experiment()
    exp.tasks = lambda: rows
    client = WithSession(optimize_model("Create the file port.txt and write the number 8080 in it, with nothing else in the file."))
    ctx = Context(client=client, power_limit_w=400.0, plumbing=True)
    done = run_experiment(exp, ctx, 2)
    assert {t.arm for t in done} == {"A", "B", "A~aa"} and not any(t.invalid for t in done)
    text = render_report(exp, ctx, done, 2)
    assert "Optimize prompt" in text and "--optimize-prompt on" in text and "protected_kept" in text


# -- E5 ------------------------------------------------------------------------------------------------------------------

def test_e5_a_small_long_turn_runs_scores_and_the_arm_sets_compaction(monkeypatch):
    variant = {"id": "mini", "modules": 6, "seed": 3}
    repo = long_turn_repo.build(6, 3)
    files, order, answer = perfect(repo)
    changed = [(p, files[p]) for p in sorted(files) if files[p] != repo.files.get(p)]
    steps = writes(repo.files, changed) + [say(answer)]
    seen = {}
    real = e5_compaction.open_agent

    def spy(workspace, client, data, **kw):
        seen[len(seen)] = kw
        return real(workspace, client, data, **kw)
    monkeypatch.setattr(e5_compaction, "open_agent", spy)
    arms = {a.name: a for a in e5_compaction.ARMS}
    on = e5_compaction.run_long(arms["B"], variant, 1, Context(client=Fake(Steps(*copy.deepcopy(steps))), power_limit_w=400.0))
    off = e5_compaction.run_long(arms["A"], variant, 1, Context(client=Fake(Steps(*copy.deepcopy(steps))), power_limit_w=400.0))
    assert seen[0]["compaction"] is True and seen[1]["compaction"] is False and seen[0]["context_window"] == seen[1]["context_window"] == 16384
    for t in (on, off):
        assert t.success and t.metrics["rule_obeyed"] == 1.0 and t.metrics["typos_left"] == 0 and t.metrics["ledger_ok"] == 1.0 and t.metrics["compactions"] == 0
    assert on.metrics["reread_tokens"] == on.metrics["prompt_tokens"] - on.metrics["cached_tokens"]
    small = e5_compaction.run_long(arms["B"], variant, 1, Context(client=Fake(Steps(*copy.deepcopy(steps), *[say("ok")] * 6)), power_limit_w=400.0, extra={"window": 8192}))
    assert seen[2]["context_window"] == 8192 and small.metrics["compactions"] >= 1               # a window the job outgrows makes the agent summarise


def test_e5_a_run_that_stops_early_is_a_failed_trial_that_says_what_is_left():
    variant = {"id": "mini", "modules": 6, "seed": 3}
    trial = e5_compaction.run_long(e5_compaction.ARMS[1], variant, 1, Context(client=Fake(Steps(say("I looked at it and it seems fine."))), power_limit_w=400.0))
    assert not trial.success and any("misspelling" in n for n in trial.notes) and trial.metrics["rule_obeyed"] == 0.0


# -- E9 ------------------------------------------------------------------------------------------------------------------

def test_e9_the_probes_have_known_answers_and_the_arms_set_compaction_and_the_agent_has_no_tool_for_them(monkeypatch):
    variant = {"id": "mini", "modules": 24, "seed": 3}
    repo = long_turn_repo.build(24, 3)
    probes = context_probes.build(repo, 24)
    assert [p["group"] for p in probes].count("incidental") == 6 or len(probes) >= 8
    assert probes[-1]["expect"] == "frozen" and probes[-2]["expect"] == long_turn_repo.FINAL_LINE
    assert context_probes.right({"kind": "yesno", "id": "t", "expect": "yes"}, "Yes, it did.") and not context_probes.right({"kind": "yesno", "id": "t", "expect": "no"}, "Yes")
    assert context_probes.right({"kind": "number", "id": "x", "expect": "7"}, "It has 7.") and not context_probes.right({"kind": "number", "id": "x", "expect": "7"}, "It has 17 or 70")
    assert context_probes.right({"kind": "text", "id": "rule-frozen", "expect": "frozen"}, "The Frozen folder")
    files, order, answer = perfect(repo)
    changed = [(p, files[p]) for p in sorted(files) if files[p] != repo.files.get(p)]
    work = writes(repo.files, changed) + [say(answer)]
    seen = []
    real = e9_context.open_agent
    monkeypatch.setattr(e9_context, "open_agent", lambda workspace, client, data, **kw: seen.append(kw) or real(workspace, client, data, **kw))
    arms = {a.name: a for a in e9_context.ARMS}
    knows = Fake(Steps(*copy.deepcopy(work), *[say(("Yes. " if p["expect"] == "yes" else "No. " if p["expect"] == "no" else "It is ") + str(p["expect"])) for p in probes]))
    forgot = Fake(Steps(*copy.deepcopy(work), *[say("I do not remember.")] * len(probes)))
    good = e9_context.run_trial(arms["A"], variant, 1, Context(client=knows, power_limit_w=400.0, extra={"window": 400000}))
    bad = e9_context.run_trial(arms["N"], variant, 1, Context(client=forgot, power_limit_w=400.0, extra={"window": 400000}))
    assert seen[0]["compaction"] is True and seen[1]["compaction"] is False
    assert good.success and good.metrics["recall"] == 1.0 and good.metrics["recall_task"] == 1.0 and good.metrics["probes_right"] == len(probes)
    assert bad.metrics["recall"] == 0.0 and bad.metrics["recall_task"] == 0.0 and any("probes answered wrong" in n for n in bad.notes)
    assert all(not r.get("tools") for r in knows.requests[-len(probes):])                          # no tool is left to answer a probe with
    assert good.metrics["reread_tokens"] == good.metrics["prompt_tokens"] - good.metrics["cached_tokens"]


def test_e9_a_run_that_stops_early_asks_no_probe_and_the_experiment_is_registered():
    variant = {"id": "mini", "modules": 24, "seed": 3}
    trial = e9_context.run_trial(e9_context.ARMS[1], variant, 1, Context(client=Fake(Steps(say("It seems fine."))), power_limit_w=400.0))
    assert trial.metrics["recall"] in (0.0, 1.0) and trial.metrics["probes_right"] <= 10
    from tadamoo.eval import cases
    exp = cases.load("e9")
    assert [a.name for a in exp.arms] == ["N", "A", "N~aa"] and exp.rule.baseline == "N" and exp.rule.challengers == ("A",)
    assert e9_context.generation_of([{"role": "user", "content": "[Compacted record: generation 2; 3 messages, about 900 tokens, written here in about 90]"}, {"role": "user", "content": "x"}]) == 2


def test_e9_the_new_columns_are_measured_and_set_in_a_trial():
    variant = {"id": "mini", "modules": 24, "seed": 3}
    repo = long_turn_repo.build(24, 3)
    files, order, answer = perfect(repo)
    changed = [(p, files[p]) for p in sorted(files) if files[p] != repo.files.get(p)]
    probes = context_probes.build(repo, 24)
    ledger = [c for c in changed if c[0] == "LEDGER.md"]
    trial = e9_context.run_trial(e9_context.ARMS[1], variant, 1, Context(client=Fake(Steps(*writes(repo.files, changed), say(answer), *[say("I do not remember.")] * len(probes))),
                                                                         power_limit_w=400.0, extra={"window": 400000}))
    assert {"summary_cut", "carry", "artifact_reads"} <= set(trial.metrics) and {"summary_cut", "carry", "artifact_reads"} <= set(e9_context.COLUMNS)
    assert trial.metrics["summary_cut"] == 0 and trial.metrics["artifact_reads"] == 0
    assert trial.metrics["carry"] == 1.0 and ledger                                  # the ledger lines it wrote are still in the conversation


def test_e9_carry_counts_the_ledger_lines_that_can_still_be_read():
    repo = long_turn_repo.build(24, 3)
    names = [f"mod_{i:03d}" for i in range(12)]
    lines = "\n".join(f"{n}: {repo.functions[n]} functions" for n in names[:6])
    call = {"id": "c", "type": "function", "function": {"name": "edit_file", "arguments": json.dumps({"path": "LEDGER.md", "replacement": lines})}}
    messages = [{"role": "system", "content": "mod_011: 1 functions"}, {"role": "assistant", "content": "", "tool_calls": [call]},
                {"role": "tool", "tool_call_id": "c", "name": "edit_file", "content": f"{names[7]}: {repo.functions[names[7]] + 1} functions"}]
    assert e9_context.carry_of(repo, messages) == 6 / 12                                # the system message and a wrong count do not count
    assert e9_context.carry_of(repo, []) == 0.0


def test_e9_artifact_reads_count_the_calls_that_go_to_the_saved_copies():
    class Call:
        def __init__(self, **arguments):
            self.arguments = arguments

    class Turn:
        def __init__(self, *calls):
            self.tool_calls = calls
    back = chr(92)
    turns = [Turn(Call(path=".tadamoo/artifacts/read_file_ab.txt"), Call(path="pkg/mod_001.py")),
             Turn(Call(command="python -c open('.basalt" + back + "artifacts" + back + "x.txt')"), Call(pattern="x", path=".tadamoo/artifacts"))]
    assert e9_context.artifact_reads(turns) == 3


def test_e9_the_large_job_is_deterministic_forbids_scripts_and_is_sized_for_a_65536_window():
    from tadamoo.stack import ANSWER_RESERVE_TOKENS, context_budget
    from tadamoo.eval.cases.core import selected_tasks
    big = e9_context.LARGE
    a, b = long_turn_repo.build(big["modules"], big["seed"]), long_turn_repo.build(big["modules"], big["seed"])
    assert a.files == b.files and len(a.functions) == 200
    budget = context_budget(65536, max(4096, min(ANSWER_RESERVE_TOKENS, 65536 // 4)))
    assert long_turn_repo.raw_tokens(a) >= 2.5 * budget
    asked = long_turn_repo.request(big["modules"], big["per_module"])
    assert "read_file and edit_file only" in asked and "Do not write or run scripts" in asked
    assert long_turn_repo.request(45) == long_turn_repo.request(45, False) and "read_file" not in long_turn_repo.request(45)          # the existing job is as it was
    exp = cases.load("e9")
    assert [t["id"] for t in selected_tasks(exp)] == ["long60", "long45"]
    assert [t["id"] for t in selected_tasks(exp, ["long200"])] == ["long200"]
    assert [t["id"] for t in selected_tasks(exp, ["long45"])] == ["long45"]


# -- E6 ------------------------------------------------------------------------------------------------------------------

VOCAB = [f"{a}{b}" for a in "bcdfgklmnprstv" for b in ("a", "e", "i", "o", "u", "al", "or", "en", "is")] * 3


def writer(seed=0):
    rng = random.Random(seed)

    def fn(messages, tools, kw):
        last = messages[-1]["content"]
        if "Plan a text of about" in last:
            n = int(re.search(r"in (\d+) sections", last).group(1))
            total = int(re.search(r"about (\d+) words", last).group(1))
            return say(json.dumps({"title": "T", "sections": [{"heading": f"S{i}", "goal": "g", "target_words": total // n, "key_points": ["a"]} for i in range(n)]}))
        if "Source of this part" in last:
            return say(last.split("-----\n")[1].rsplit("\n-----", 1)[0])             # an identity "translation": markers copied as they came
        n = int(re.search(r"about (\d+) words", last).group(1))
        words = [rng.choice(VOCAB) + rng.choice(["", "", str(rng.randint(0, 99))]) for _ in range(n)]
        sentences = [" ".join(words[i:i + 14]) + "." for i in range(0, n, 14)]
        return say("\n\n".join(" ".join(sentences[i:i + 5]) for i in range(0, len(sentences), 5)))
    return fn


def test_e6_each_way_delivers_the_words_and_the_arm_sets_the_number_of_parts():
    task = next(t for t in e6_longform.tasks() if t["id"] == "write3000")
    arms = {a.name: a for a in e6_longform.ARMS}
    got = {n: e6_longform.run_text(arms[n], task, 1, Context(client=Fake(writer(1)), power_limit_w=400.0)) for n in ("A", "B", "D", "E")}
    assert got["B"].metrics["parts"] == 3 and got["E"].metrics["parts"] == 1 and got["A"].metrics["parts"] == 2 and got["D"].metrics["parts"] == 2
    for name, t in got.items():
        assert 0.85 <= t.metrics["delivered_ratio"] <= 1.3 and t.success, (name, t.notes)
    assert got["E"].metrics["seam_issues"] == 0 and got["A"].metrics["warnings"] == 0 and got["B"].metrics["prompt_tokens"] > 0


def test_e6_a_translation_keeps_the_protected_content_byte_for_byte_and_plan_does_not_apply():
    task = next(t for t in e6_longform.tasks() if t["kind"] == "translate")
    arms = {a.name: a for a in e6_longform.ARMS}
    t = e6_longform.run_text(arms["B"], task, 1, Context(client=Fake(writer(2)), power_limit_w=400.0))
    assert t.metrics["protected_intact"] == 1.0 and t.metrics["parts"] >= 3 and t.metrics["language_ok"] == 0.0 and not t.success      # an identity "translation" stays English
    skipped = e6_longform.run_text(arms["D"], task, 1, Context(client=Fake(writer(2))))
    assert skipped.invalid and "nothing to plan" in skipped.error


def test_e6_the_experiment_runs_and_reports_all_arms():
    exp = e6_longform.experiment()
    exp.tasks = lambda: [t for t in e6_longform.tasks() if t["id"] == "write3000"]
    done = run_experiment(exp, Context(client=Fake(writer(3)), power_limit_w=400.0), 1)
    assert {t.arm for t in done} == {"A", "B", "C", "D", "E", "A~aa"}
    text = render_report(exp, Context(power_limit_w=400.0), done, 1)
    assert "parts of 1000 words" in text and "Rule, written before the run" in text and text.count("against A") >= 4


# -- E7 ------------------------------------------------------------------------------------------------------------------

class PrefixServer:
    """A server with `slots` prefix caches, least recently used out: it reports the words a request shares with a cached prompt as cached tokens."""

    def __init__(self, slots=1):
        self.slots, self.cache = slots, []

    def chat_completion(self, messages, tools=None, on_delta=None, **kw):
        words = (json.dumps(tools) + " " + " ".join(f"<{m['role']}> {m['content']}" for m in messages)).split()
        best, hit = None, 0
        for c in self.cache:
            n = 0
            while n < min(len(c), len(words)) and c[n] == words[n]:
                n += 1
            if n > hit:
                best, hit = c, n
        if best is not None:
            self.cache.remove(best)
        self.cache = ([words] + self.cache)[: self.slots]
        if on_delta:
            on_delta("content", "ok")
        return {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": len(words), "prompt_tokens_details": {"cached_tokens": hit}}}


def e7_shares(slots):
    exp = e7_cache.experiment()
    exp.tasks = lambda: [{"id": "prefix800", "words": 800}]
    done = run_experiment(exp, Context(client=PrefixServer(slots), power_limit_w=400.0), 2)
    grouped = {}
    for t in done:
        grouped.setdefault(t.arm, []).append(t.metrics["cached_share"])
    return exp, done, {k: sum(v) / len(v) for k, v in grouped.items()}


def test_e7_a_single_slot_server_loses_the_cache_to_a_side_call_and_two_slots_keep_it():
    exp, done, one = e7_shares(1)
    assert one["A"] > 0.95 and one["A~aa"] > 0.95 and one["G"] < 0.4
    assert one["B"] > 0.9                                     # a new turn extends the cached prompt
    assert one["C"] < one["A"] - 0.2 and one["D"] < 0.4 and 0.3 < one["E"] < 0.8
    assert one["F"] < 0.4                                     # the side call took the only slot
    _, _, two = e7_shares(2)
    assert two["F"] > 0.9
    text = render_report(exp, Context(power_limit_w=400.0), done, 2, {"Server setup": "parallel=1"})
    assert "Survival of the prefix cache" in text and "Server setup" in text and "`F` a short side call between two turns" in text


# -- the command line ----------------------------------------------------------------------------------------------------

def test_the_command_line_lists_everything_and_needs_the_power_limit_for_a_run_with_the_model(capsys):
    from tadamoo.eval.cases.__main__ import main
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    for key in cases.MODULES:
        assert out.count(f"\n{key}  ") + out.startswith(f"{key}  ") == 1
    assert "[--verify-gate always" in out and "--no-error-classifier" in out and "--intake full" in out
    with pytest.raises(SystemExit):
        main(["e2"])
    assert "--gpu-power-limit-w" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["e2", "--gpu-power-limit-w", "400", "--arms", "Z"])


def test_the_command_line_runs_e3_without_a_model_and_writes_the_report(tmp_path, capsys):
    from tadamoo.eval.cases.__main__ import main
    out, js = tmp_path / "e3.md", tmp_path / "e3.json"
    assert main(["e3", "--k", "1", "--only", "transient_429_500", "--only", "deterministic_401", "--deadline-s", "0.8", "--out", str(out), "--json", str(js)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "Plumbing check" in text and "`A~aa`" in text and json.loads(js.read_text(encoding="utf-8"))["plumbing"] is True
    assert len(json.loads(js.read_text(encoding="utf-8"))["trials"]) == 6
