"""The Target Loop (harness/target.py): the split into criteria and tasks, the passes, the judge, the stall rule, parking, stopping."""
import json
import threading
import time

import pytest

from harness import target as tg
from harness.client import BasaltError
from serve.tasks import TaskJournal


class FakeLLM:
    """Answers the structured calls from queues by name; a callable in a queue is given the user text. Records every call."""

    def __init__(self, **queues):
        self.queues = {k: list(v) for k, v in queues.items()}
        self.calls = []

    def ask(self, name, system, user, schema, temperature=0.0):
        self.calls.append((name, user))
        queue = self.queues.get(name) or []
        if not queue:
            raise AssertionError(f"no scripted answer for {name}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        item = item(user) if callable(item) else item
        if isinstance(item, Exception):
            raise item
        return item

    def of(self, name):
        return [u for n, u in self.calls if n == name]


class FakeConversation:
    def __init__(self, answer=None, files=(), changes=None):
        self.prompts, self.events, self.cancelled = [], [], 0
        self.answer = answer or (lambda prompt: tg.TurnResult(final="done: " + prompt.splitlines()[0][:40]))
        self.files = set(files)
        self.edits = changes or []

    def run(self, prompt, cancel):
        self.prompts.append(prompt)
        return self.answer(prompt)

    def cancel(self):
        self.cancelled += 1

    def file_exists(self, path):
        return path in self.files

    def parked(self):
        return []

    def changes_since(self, since):
        return list(self.edits)

    def say(self, event):
        self.events.append(event)


def plan(criteria, tasks):
    return {"criteria": [{"id": i, "text": t} for i, t in criteria], "tasks": tasks}


def task(tid, title, serves, depends=(), subtasks=()):
    return {"id": tid, "title": title, "prompt": "do " + title, "serves": list(serves), "depends_on": list(depends), "subtasks": list(subtasks)}


def verdict(cid, status, evidence="The tool output shows it.", refs=("E1",), missing=""):
    return {"id": cid, "status": status, "evidence": evidence, "evidence_refs": list(refs), "missing": missing}


def verdicts(*items):
    return {"verdicts": list(items)}


def fix(*tasks, analysis=()):
    return {"analysis": [{"id": i, "why": w, "change": c} for i, w, c in analysis], "tasks": list(tasks)}


@pytest.fixture
def journal(tmp_path):
    return TaskJournal(tmp_path / "journal")


def run_loop(llm, conv, registry, text="Make A and B.", options=None, waits=(0.01,)):
    target = tg.new_target(text, options, session="s1")
    saved = []
    loop = tg.TargetLoop(target, conv, llm, registry, lambda t: saved.append(json.dumps(t)), waits=waits)
    loop.run()
    return target, loop, saved


# -- the split -----------------------------------------------------------------------------------------------------

def test_the_split_makes_ids_resolves_dependencies_and_breaks_a_cycle():
    data = plan([("c1", "A exists"), ("c2", "B exists")], [
        task("x", "Write A", ["c1"], depends=["y"], subtasks=[task("x.a", "part one", [], depends=["x.b"]), task("x.b", "part two", [])]),
        task("y", "Write B", ["c2", "nope"], depends=["x", "ghost"])])
    target = tg.new_target("t")
    tg.apply_plan(target, data)
    by = {t["id"]: t for t in target["tasks"]}
    assert [t["id"] for t in target["tasks"]] == ["t1", "t1.1", "t1.2", "t2"]
    assert by["t1.1"]["parent"] == "t1" and by["t1.1"]["serves"] == ["c1"]             # a sub-task serves what its group serves
    assert by["t1.1"]["depends_on"] == ["t1.2"] and by["t2"]["serves"] == ["c2"]
    assert not (by["t1"]["depends_on"] == ["t2"] and by["t2"]["depends_on"] == ["t1"])      # the cycle was cut
    assert "ghost" not in by["t2"]["depends_on"]


def test_an_unusable_split_still_leaves_one_criterion_and_one_task():
    target = tg.new_target("Do the thing")
    tg.apply_plan(target, {})
    assert [c["text"] for c in target["criteria"]] == ["Do the thing"] and len(target["tasks"]) == 1
    assert target["tasks"][0]["serves"] == ["c1"]


def test_tasks_go_in_dependency_order_and_groups_wait_for_their_parts():
    target = tg.new_target("t")
    tg.apply_plan(target, plan([("c1", "A"), ("c2", "B")], [task("a", "A", ["c1"], depends=["b"]), task("b", "B", ["c2"], subtasks=[task("b1", "B1", [])])]))
    order = []
    while (t := tg.next_task(target["tasks"])):
        order.append(t["id"])
        t["status"] = "done"
    assert order == ["t2.1", "t1"]                                                      # B's part first, then A, which needs B


def test_a_text_that_is_empty_or_far_too_long_is_refused():
    with pytest.raises(tg.TargetError) as e:
        tg.new_target("   ")
    assert e.value.code == "text_missing"
    with pytest.raises(tg.TargetError):
        tg.new_target("x" * (tg.TEXT_CHARS + 1))


# -- one pass --------------------------------------------------------------------------------------------------------

def test_everything_achieved_in_one_pass_closes_the_loop_and_fills_the_registry(journal):
    llm = FakeLLM(target_plan=[plan([("c1", "A exists"), ("c2", "B exists")], [
        task("a", "Write A", ["c1"]), task("b", "Write B", ["c2"], subtasks=[task("b1", "B part", [])])])],
        target_verdict=[lambda user: verdicts(verdict("c1", "achieved", refs=("E1",)), verdict("c2", "achieved", refs=("E2",)))])
    conv = FakeConversation()
    target, _, saved = run_loop(llm, conv, journal, text="Make A and B.")
    assert target["state"] == "achieved" and target["stop_reason"] == "" and len(target["iterations"]) == 1
    assert [c["status"] for c in target["criteria"]] == ["achieved", "achieved"]
    assert target["summary"]["achieved"] == 2 and target["summary"]["passes"] == 1
    assert len(conv.prompts) == 2 and len(llm.of("target_verdict")) == 1 and not llm.of("target_fix")
    assert conv.prompts[0].startswith("Make A and B.\n\n---\nTarget Loop") and "Target:" not in conv.prompts[0].split("---", 1)[1]      # the text is the person's message
    assert conv.prompts[1].startswith("Target Loop · task 2 of 2") and "Target:\nMake A and B." in conv.prompts[1]
    registry = {t["id"]: t for t in journal.load()["tasks"]}
    assert registry[f"tg-{target['id']}-t2"]["children"] == [f"tg-{target['id']}-t2.1"]
    assert registry[f"tg-{target['id']}-t1"]["status"] == "done" and registry[f"tg-{target['id']}-t2.1"]["status"] == "done"
    assert registry[f"tg-{target['id']}-t1"]["metadata"]["kind"] == "target"
    assert saved and [e["what"] for e in conv.events][-1] == "achieved"


def test_a_focused_prompt_holds_only_what_the_task_needs():
    target = tg.new_target("L" * 3000)
    tg.apply_plan(target, plan([("c1", "A"), ("c2", "B"), ("c3", "C")], [task("a", "A", ["c1"]), task("b", "B", ["c2"], depends=["a"])]))
    target["criteria"][2].update(status="achieved")
    target["criteria"][1]["note"] = "use the new format"
    target["tasks"][0].update(status="done", result="wrote a.txt")
    text = tg.focused_prompt(target, target["tasks"][1], (2, 2), False)
    assert "c2: B" in text and "c1: A" not in text                                         # only the criterion it serves
    assert "Already achieved, do not redo: c3" in text and "a: " not in text.split("Results")[0]
    assert "- A: wrote a.txt" in text and "use the new format" in text
    assert text.count("L") < 1700 and "earlier in this conversation" in text                 # a long text is cut, the conversation holds it


# -- the judge ---------------------------------------------------------------------------------------------------------

def test_achieved_without_cited_evidence_is_downgraded_to_partial(journal):
    llm = FakeLLM(target_plan=[plan([("c1", "A"), ("c2", "B"), ("c3", "C")], [task("a", "A", ["c1", "c2", "c3"])])],
                  target_verdict=[verdicts(verdict("c1", "achieved", refs=()), verdict("c2", "achieved", evidence="ok", refs=("E1",)), verdict("c3", "achieved", refs=("E99",))),
                                  verdicts(verdict("c1", "achieved", refs=("E1",)), verdict("c2", "achieved", refs=("E1",)), verdict("c3", "achieved", refs=("E1",)))],
                  target_fix=[fix(task("n", "Retry", ["c1", "c2", "c3"]))])
    target, _, _ = run_loop(llm, FakeConversation(), journal)
    first = target["iterations"][0]["verdict"]
    assert first["partial"] == 3 and first["achieved"] == 0                      # no ids, evidence too short, an id that is not in the evidence
    assert target["state"] == "achieved" and len(target["iterations"]) == 2


def test_the_judge_is_asked_only_about_the_unmet_criteria_and_achieved_work_is_not_redone(journal):
    llm = FakeLLM(target_plan=[plan([("c1", "A exists"), ("c2", "B has tests")], [task("a", "Write A", ["c1"]), task("b", "Write B", ["c2"])])],
                  target_verdict=[verdicts(verdict("c1", "achieved"), verdict("c2", "partial", missing="no test for the empty case")),
                                  verdicts(verdict("c2", "achieved", refs=("E1",)))],
                  target_fix=[fix(task("n", "Add the empty-case test", ["c2", "c1"]), analysis=[("c2", "the empty case was never tried", "add a test for it")])])
    conv = FakeConversation()
    target, _, _ = run_loop(llm, conv, journal)
    assert target["state"] == "achieved" and len(target["iterations"]) == 2
    second_judge = llm.of("target_verdict")[1]
    assert "c2: B has tests" in second_judge.split("EVIDENCE")[0].split("CRITERIA TO JUDGE:")[1] and "c1:" not in second_judge.split("EVIDENCE")[0].split("CRITERIA TO JUDGE:")[1]
    assert len(conv.prompts) == 3 and "Add the empty-case test" in conv.prompts[2] and "Already achieved, do not redo: c1" in conv.prompts[2]
    new = [t for t in target["tasks"] if t["round"] == 2]
    assert [t["id"] for t in new] == ["r2.1"] and new[0]["serves"] == ["c2"]                  # it serves only what was unmet
    assert target["iterations"][0]["next"]["analysis"][0]["change"] == "add a test for it"
    assert "no test for the empty case" in llm.of("target_fix")[0]


def test_a_task_that_fails_is_recorded_and_the_judge_decides(journal):
    llm = FakeLLM(target_plan=[plan([("c1", "A")], [task("a", "A", ["c1"])])],
                  target_verdict=[verdicts(verdict("c1", "not_met", missing="the command failed")), verdicts(verdict("c1", "achieved"))],
                  target_fix=[fix(task("n", "Again", ["c1"]))])
    answers = [tg.TurnResult(stop_reason="error", error="model failed"), tg.TurnResult(final="fixed")]
    target, _, _ = run_loop(llm, FakeConversation(answer=lambda p: answers.pop(0)), journal)
    assert target["tasks"][0]["status"] == "failed" and target["state"] == "achieved"
    assert journal_status(journal, target, "t1") == "failed"


def journal_status(journal, target, local):
    return next(t for t in journal.load()["tasks"] if t["id"] == f"tg-{target['id']}-{local}")["status"]


# -- checks --------------------------------------------------------------------------------------------------------------

def shell_result(code, out=""):
    return json.dumps({"exit_code": code, "stdout": out, "stderr": "", "success": code == 0, "timed_out": False})


def test_a_command_and_a_file_the_person_asked_for_decide_by_themselves(journal):
    outcomes = [1, 0]

    def answer(prompt):
        if prompt.startswith("Target Loop · check"):
            code = outcomes.pop(0)
            return tg.TurnResult(final="ran", tools=[{"name": "shell_command", "arguments": {"command": "pytest -q"}, "status": "completed", "output": shell_result(code, "1 failed" if code else "all passed")}])
        return tg.TurnResult(final="worked")
    llm = FakeLLM(target_plan=[plan([("c1", "A")], [task("a", "A", ["c1"])])],
                  target_verdict=[verdicts(verdict("c1", "achieved"))],
                  target_fix=[fix(task("n", "Fix the failing test", ["k1"]))])
    conv = FakeConversation(answer=answer, files={"out.txt"})
    target, _, _ = run_loop(llm, conv, journal, options={"verify": ["pytest -q"], "files": ["out.txt"]})
    by = {c["id"]: c for c in target["criteria"]}
    assert by["k1"]["kind"] == "check" and by["k1"]["status"] == "achieved" and by["k2"]["status"] == "achieved"
    assert target["state"] == "achieved" and len(target["iterations"]) == 2          # the failed command kept the loop going although the judge was satisfied
    assert [c["ok"] for c in target["iterations"][0]["checks"]] == [False, True] and "1 failed" in target["iterations"][0]["checks"][0]["output"]
    first_judge = llm.of("target_verdict")[0]
    assert "[failed]" in first_judge and "1 failed" in first_judge                       # the judge sees the failed command
    assert "Fix the failing test" in conv.prompts[-2]


def test_a_file_that_never_appears_keeps_its_criterion_unmet(journal):
    llm = FakeLLM(target_plan=[plan([("c1", "A")], [task("a", "A", ["c1"])])], target_verdict=[verdicts(verdict("c1", "achieved"))], target_fix=[fix(task("n", "Make it", ["k1"]))])
    target, _, _ = run_loop(llm, FakeConversation(), journal, options={"files": ["gone.txt"], "max_iterations": 2})
    k1 = target["criteria"][1]
    assert k1["id"] == "k1" and k1["status"] == "not_met" and k1["missing"] == "missing"
    assert target["state"] == "stopped" and target["stop_reason"] == "limit"


def test_a_check_the_conversation_did_not_run_counts_as_failed():
    ok, text = tg.command_outcome(tg.TurnResult(final="I ran it", tools=[]), "pytest")
    assert ok is False and "did not run" in text
    ok, _ = tg.command_outcome(tg.TurnResult(tools=[{"name": "shell_command", "arguments": {"command": "pytest  -q"}, "status": "completed", "output": shell_result(0)}]), "pytest -q")
    assert ok is True


# -- stopping on a stall ---------------------------------------------------------------------------------------------------

def test_the_same_unmet_item_with_the_same_evidence_asks_for_a_different_approach_once_then_stops(journal):
    same = verdicts(verdict("c1", "not_met", missing="the build still fails"))
    llm = FakeLLM(target_plan=[plan([("c1", "builds")], [task("a", "Build", ["c1"])])], target_verdict=[same],
                  target_fix=[fix(task("n", "Try the same thing", ["c1"]), analysis=[("c1", "w", "try the same thing")])])
    conv = FakeConversation()
    target, _, _ = run_loop(llm, conv, journal, options={"stall_passes": 3})
    assert target["state"] == "stopped" and target["stop_reason"] == "stalled" and target["approach_tried"] is True
    assert len(target["iterations"]) == 6                                         # 3 passes, a different approach, 3 more passes, then it stops
    assert len(llm.of("target_fix")) == 5                                          # fixes after passes 1, 2, 4 and 5, and the different approach after pass 3
    assert "CHANGES TRIED SO FAR" in llm.of("target_fix")[2] and "CHANGES TRIED SO FAR" not in llm.of("target_fix")[1]
    assert [e["what"] for e in conv.events].count("approach") == 1
    leftovers = [t for t in journal.load()["tasks"] if t["status"] in ("pending", "running")]
    assert leftovers == []


def test_a_new_approach_that_changes_the_evidence_goes_on_without_stopping(journal):
    verdicts_seq = [verdicts(verdict("c1", "not_met", missing="fails because of A"))] * 2 + [verdicts(verdict("c1", "not_met", missing="fails because of B"))] + [verdicts(verdict("c1", "achieved"))]
    llm = FakeLLM(target_plan=[plan([("c1", "builds")], [task("a", "Build", ["c1"])])], target_verdict=verdicts_seq,
                  target_fix=[fix(task("n", "again", ["c1"]))])
    target, _, _ = run_loop(llm, FakeConversation(), journal, options={"stall_passes": 2})
    assert target["state"] == "achieved" and target["approach_tried"] is True and len(target["iterations"]) == 4


def test_the_person_may_cap_the_passes_and_nothing_else_caps_them(journal):
    changing = [lambda user, n=n: verdicts(verdict("c1", "not_met", missing=f"missing part {chr(97 + n)}")) for n in range(8)]
    llm = FakeLLM(target_plan=[plan([("c1", "x")], [task("a", "x", ["c1"])])], target_verdict=changing, target_fix=[fix(task("n", "more", ["c1"]))])
    target, _, _ = run_loop(llm, FakeConversation(), journal, options={"max_iterations": 3})
    assert target["state"] == "stopped" and target["stop_reason"] == "limit" and len(target["iterations"]) == 3


# -- parking -------------------------------------------------------------------------------------------------------------------

def test_a_blocked_criterion_is_parked_and_the_rest_goes_on(journal):
    llm = FakeLLM(target_plan=[plan([("c1", "site is built"), ("c2", "table is booked")], [task("a", "Build the site", ["c1"]), task("b", "Book the table", ["c2"])])],
                  target_verdict=[verdicts(verdict("c1", "partial", missing="CSS missing"), verdict("c2", "blocked", missing="the person has to phone the restaurant")),
                                  verdicts(verdict("c1", "achieved"))],
                  target_fix=[fix(task("n", "Add the CSS", ["c1", "c2"]))])
    conv = FakeConversation()
    target, _, _ = run_loop(llm, conv, journal)
    assert target["state"] == "blocked" and target["stop_reason"] == ""
    c1, c2 = target["criteria"]
    assert c1["status"] == "achieved" and c2["status"] == "blocked" and "phone" in c2["missing"]
    assert len(llm.of("target_verdict")) == 2 and "c2:" not in llm.of("target_verdict")[1].split("EVIDENCE")[0].split("CRITERIA TO JUDGE:")[1]
    assert [t["serves"] for t in target["tasks"] if t["round"] == 2] == [["c1"]]                    # the new tasks leave the parked criterion alone
    assert journal_status(journal, target, "t2") == "done"                                         # it ran in the first pass, before the verdict


def test_tasks_that_only_serve_a_parked_criterion_wait_in_the_registry(journal):
    target = tg.new_target("t", session="s")
    tg.apply_plan(target, plan([("c1", "A"), ("c2", "B")], [task("a", "A", ["c1"]), task("b", "B", ["c2"])]))
    target["criteria"][1]["status"] = "blocked"
    loop = tg.TargetLoop(target, FakeConversation(), FakeLLM(), journal, lambda t: None)
    loop._park_blocked()
    assert [t["status"] for t in target["tasks"]] == ["pending", "blocked"]
    assert tg.next_task(target["tasks"])["id"] == "t1"


# -- stopping on the person's word --------------------------------------------------------------------------------------------------

def test_stopping_mid_run_ends_the_target_and_puts_the_task_back(journal):
    cancel = threading.Event()

    class Slow(FakeConversation):
        def run(self, prompt, cancel_event):
            self.prompts.append(prompt)
            started.set()
            cancel_event.wait(10)
            return tg.TurnResult(stop_reason="cancelled")
    started = threading.Event()
    llm = FakeLLM(target_plan=[plan([("c1", "A"), ("c2", "B")], [task("a", "A", ["c1"]), task("b", "B", ["c2"])])])
    target = tg.new_target("t", session="s")
    loop = tg.TargetLoop(target, Slow(), llm, journal, lambda t: None, cancel=cancel)
    thread = threading.Thread(target=loop.run)
    thread.start()
    assert started.wait(5)
    cancel.set()
    thread.join(5)
    assert not thread.is_alive()
    assert target["state"] == "stopped" and target["stop_reason"] == "person"
    assert [t["status"] for t in target["tasks"]] == ["pending", "pending"] and not llm.of("target_verdict")
    assert [t for t in journal.load()["tasks"] if t["status"] in ("pending", "running")] == []        # what was not done leaves the registry


def test_a_model_that_is_not_reachable_is_waited_for_and_a_stop_ends_the_wait(journal):
    cancel = threading.Event()
    llm = FakeLLM(target_plan=[BasaltError(0, "no model")])
    target = tg.new_target("t", session="s")
    loop = tg.TargetLoop(target, FakeConversation(), llm, journal, lambda t: None, cancel=cancel, waits=(0.01,))
    thread = threading.Thread(target=loop.run)
    thread.start()
    deadline = time.time() + 5
    while len(llm.calls) < 3 and time.time() < deadline:
        time.sleep(0.01)
    assert target["phase"] == "waiting_model" and thread.is_alive()
    cancel.set()
    thread.join(5)
    assert target["state"] == "stopped" and target["stop_reason"] == "person"


def test_an_answer_that_is_not_usable_three_times_falls_back_to_the_plain_split(journal):
    llm = FakeLLM(target_plan=[ValueError("bad")], target_verdict=[verdicts(verdict("c1", "achieved"))])
    target, _, _ = run_loop(llm, FakeConversation(), journal, text="Do it all")
    assert len(llm.of("target_plan")) == tg.BAD_ANSWER_TRIES and target["state"] == "achieved"
    assert target["criteria"][0]["text"] == "Do it all"


def test_a_crash_in_the_loop_stops_the_target_with_a_reason(journal):
    class Broken(FakeConversation):
        def run(self, prompt, cancel):
            raise RuntimeError("boom")
    llm = FakeLLM(target_plan=[plan([("c1", "A")], [task("a", "A", ["c1"])])])
    target, _, _ = run_loop(llm, Broken(), journal)
    assert target["state"] == "stopped" and target["stop_reason"] == "error" and "boom" in target["error"]


def test_the_persons_own_status_is_left_alone_by_the_judge(journal):
    llm = FakeLLM(target_plan=[plan([("c1", "A"), ("c2", "B")], [task("a", "A", ["c1", "c2"])])],
                  target_verdict=[verdicts(verdict("c1", "not_met", missing="x"), verdict("c2", "achieved"))])
    target = tg.new_target("t", session="s")
    target["criteria"] = [tg.criterion("c1", "A"), tg.criterion("c2", "B")]
    target["criteria"][0].update(status="achieved", by="person")
    target["tasks"] = tg.plan_tasks([task("a", "A", ["c1", "c2"])], ["c1", "c2"], "t", 1, set())
    tg.TargetLoop(target, FakeConversation(), llm, journal, lambda t: None).run()
    assert target["criteria"][0]["status"] == "achieved" and target["criteria"][0]["by"] == "person" and target["state"] == "achieved"


def test_a_criterion_keeps_its_source_words_only_when_they_are_really_in_the_text():
    target = tg.new_target("1. Builds.\n2. Has a title.")
    tg.apply_plan(target, {"criteria": [{"id": "c1", "text": "Builds", "source": "1. Builds."}, {"id": "c2", "text": "Title", "source": "invented words"}],
                           "tasks": [task("a", "A", ["c1", "c2"])]})
    assert [c["source"] for c in target["criteria"]] == ["1. Builds.", ""]


# -- the first message ---------------------------------------------------------------------------------------------

def test_first_message_is_an_option_that_defaults_to_joined_and_ignores_anything_else():
    assert tg.clean_options({})["first_message"] == "joined" and tg.clean_options(None)["first_message"] == "joined"
    assert tg.clean_options({"first_message": "separate"})["first_message"] == "separate"
    assert tg.clean_options({"first_message": "both"})["first_message"] == "joined" and tg.clean_options({"first_message": 3})["first_message"] == "joined"
    assert tg.new_target("t", {"first_message": "separate"})["options"]["first_message"] == "separate"


def two_task_llm():
    return FakeLLM(target_plan=[plan([("c1", "A"), ("c2", "B")], [task("a", "Write A", ["c1"]), task("b", "Write B", ["c2"])])],
                   target_verdict=[verdicts(verdict("c1", "achieved", refs=("E1",)), verdict("c2", "achieved", refs=("E2",)))])


def test_joined_sends_the_text_with_the_first_task_and_separate_sends_it_alone_then_one_message_per_task(journal):
    joined = FakeConversation()
    target, _, _ = run_loop(two_task_llm(), joined, journal, text="Make A and B.", options={"first_message": "joined"})
    assert target["state"] == "achieved" and len(joined.prompts) == 2
    assert joined.prompts[0].startswith("Make A and B.\n\n---\nTarget Loop · task 1 of 2")
    alone = FakeConversation()
    target, _, _ = run_loop(two_task_llm(), alone, journal, text="Make A and B.", options={"first_message": "separate"})
    assert target["state"] == "achieved" and target["posted"] is True and len(alone.prompts) == 3
    assert alone.prompts[0] == "Make A and B."
    assert alone.prompts[1].startswith("Target Loop · task 1 of 2") and "Target:\nMake A and B." in alone.prompts[1]
    assert alone.prompts[2].startswith("Target Loop · task 2 of 2")


def test_the_text_alone_is_evidence_for_the_judge_and_is_posted_once_even_after_a_second_pass(journal):
    llm = FakeLLM(target_plan=[plan([("c1", "A")], [task("a", "Write A", ["c1"])])],
                  target_verdict=[verdicts(verdict("c1", "not_met", missing="no file")), verdicts(verdict("c1", "achieved", refs=("E1",)))],
                  target_fix=[fix(task("r", "Write A again", ["c1"]), analysis=[("c1", "nothing written", "write it")])])
    conv = FakeConversation(answer=lambda p: tg.TurnResult(final="seen: " + p.splitlines()[0][:30]))
    target, _, _ = run_loop(llm, conv, journal, text="Make A.", options={"first_message": "separate"})
    assert target["state"] == "achieved" and conv.prompts.count("Make A.") == 1 and len(conv.prompts) == 3
    assert "the target text" in llm.of("target_verdict")[0] and "seen: Make A." in llm.of("target_verdict")[0]


def test_stopping_while_the_text_alone_is_being_answered_leaves_it_unposted_so_a_resume_sends_it_again(journal):
    box = {}

    def answer(prompt):
        box["loop"].cancel.set()
        return tg.TurnResult(stop_reason="cancelled")
    conv = FakeConversation(answer=answer)
    target = tg.new_target("Make A.", {"first_message": "separate"}, session="s1")
    llm = two_task_llm()
    loop = tg.TargetLoop(target, conv, llm, journal, lambda t: None, waits=(0.01,))
    box["loop"] = loop
    loop.run()
    assert target["state"] == "stopped" and target["stop_reason"] == "person" and target["posted"] is False and conv.prompts == ["Make A."]
    assert all(t["attempts"] == 0 for t in target["tasks"])
