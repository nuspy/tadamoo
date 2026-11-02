"""The case studies (harness/eval/cases): the shared part, the fixtures, the scorers and the wiring of the arms. CPU only, no model."""
import dataclasses
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import intake as it
from harness import target as tg
from harness.eval import cases
from harness import optimize as opt
from harness.eval.cases import e1_target, e2_gate, e3_errors, e4_intake, e5_compaction, e6_longform, e7_cache, e8_optimize
from harness.eval.cases.agentrun import apply_solution, snapshot, wasted_turns, write_files
from harness.eval.cases.core import (Arm, CaseTrial, Context, Experiment, Guard, Rule, arm_summary, by_arm, decide, render_report, results_json, rotate,
                                     run_experiment, with_control)
from harness.eval.cases.fixtures import error_scenarios, gate_tasks, intake_prompts, longform_texts, long_turn_repo, optimize_prompts, shop_repo
from harness.eval.tasks import Task, make_workspace, run_checks
from harness.stack import AgentConfig
from serve import longform as lf


# -- the shared part ----------------------------------------------------------------------------------------------

def trials(arm, values, task="t"):
    return [CaseTrial(arm, task, i + 1, success=bool(v), metrics={"seconds": 10.0}) for i, v in enumerate(values)]


RULE = Rule(primary="success", better="higher", min_effect=0.10, text="x", challengers=("B",), guards=(Guard("seconds", "lower", 0.25, relative=True),))


def grouped(a, aa, b):
    return {"A": trials("A", a), "A~aa": trials("A~aa", aa), "B": trials("B", b)}


def test_a_clear_difference_beyond_the_ab_gap_wins():
    (v,) = decide(grouped([1, 0, 0, 0, 0, 0, 0, 0, 0, 0], [0, 1, 0, 0, 0, 0, 0, 0, 0, 0], [1, 1, 1, 1, 1, 1, 0, 1, 1, 0]), RULE)
    assert v.outcome == "B is better than A" and v.noise == pytest.approx(0.0) and v.effect > v.threshold


def test_a_difference_the_ab_gap_could_have_made_does_not_win():
    (v,) = decide(grouped([1, 1, 1, 1, 0, 0, 0, 0, 0, 0], [0, 0, 0, 1, 1, 1, 1, 1, 1, 1], [1, 1, 1, 1, 1, 1, 0, 0, 0, 0]), RULE)
    assert v.noise == pytest.approx(0.3) and "no difference beyond the noise" in v.outcome
    assert any("A/A gap" in c for c in v.caveats)


def test_a_small_effect_under_the_floor_is_not_a_win_even_with_no_noise():
    (v,) = decide(grouped([1] * 10, [1] * 10, [1] * 10), RULE)
    assert "no difference" in v.outcome
    (w,) = decide(grouped([0] * 20, [0] * 20, [1] + [0] * 19), RULE)                 # +5 points: under the 10-point floor
    assert "no difference" in w.outcome


def test_a_worse_challenger_is_reported_as_the_baseline_being_better():
    (v,) = decide(grouped([1] * 10, [1] * 10, [0] * 10), RULE)
    assert v.outcome == "A is better than B"


def test_a_broken_guard_stops_the_adoption():
    g = grouped([0] * 10, [0] * 10, [1] * 10)
    for t in g["B"]:
        t.metrics["seconds"] = 30.0
    (v,) = decide(g, RULE)
    assert "breaks a guard (seconds)" in v.outcome and "not adopted" in v.outcome and any("broken" in x for x in v.guards)


def test_without_the_ab_control_or_the_arm_there_is_no_verdict():
    (v,) = decide({"A": trials("A", [1] * 4), "B": trials("B", [1] * 4)}, RULE)
    assert v.outcome.startswith("no result") and "A/A" in v.outcome
    (w,) = decide({"A": trials("A", [1] * 4), "A~aa": trials("A~aa", [1] * 4)}, RULE)
    assert w.outcome.startswith("no result")


def test_lower_is_better_flips_the_sign_and_invalid_trials_are_left_out():
    rule = Rule(primary="never_ends", better="lower", min_effect=0.1, text="x")
    mk = lambda arm, v: [CaseTrial(arm, "t", i, metrics={"never_ends": x}) for i, x in enumerate(v)]
    g = {"A": mk("A", [1.0] * 6), "A~aa": mk("A~aa", [1.0] * 6), "B": mk("B", [0.0] * 6)}
    g["B"].append(CaseTrial("B", "t", 9, metrics={"never_ends": 1.0}, invalid=True))
    (v,) = decide(g, rule)
    assert v.outcome == "B is better than A" and v.challenger_value == 0.0


def test_the_control_is_the_same_arm_under_another_name_and_arms_alternate():
    arms = with_control([Arm("A", "a", {"x": 1}), Arm("B", "b", {})], "A")
    assert [a.name for a in arms] == ["A", "B", "A~aa"] and arms[2].settings == arms[0].settings and arms[2].copy_of == "A"
    assert rotate(["a", "b", "c"], 1) == ["b", "c", "a"] and rotate([], 3) == []
    firsts = {rotate(arms, n)[0].name for n in range(3)}
    assert firsts == {"A", "B", "A~aa"}


def fake_experiment(boom=False):
    seen = []

    def run(arm, task, rep, ctx):
        seen.append((rep, task, arm.name))
        if boom and arm.name == "B":
            raise RuntimeError("broken")
        return CaseTrial(arm.name, task, rep, success=arm.name == "B", metrics={"seconds": 1.0})
    exp = Experiment("ex", "Title", "A question?", with_control([Arm("A", "a"), Arm("B", "b")], "A"), RULE, ("success", "seconds"), lambda: ["t1", "t2"], run, task_id=str)
    return exp, seen


def test_every_task_runs_under_every_arm_k_times_and_a_crash_is_an_invalid_trial():
    exp, seen = fake_experiment(boom=True)
    done = run_experiment(exp, Context(power_limit_w=400.0), 2)
    assert len(done) == 2 * 2 * 3 and len(seen) == 12
    assert sum(t.invalid for t in done) == 4 and all("RuntimeError" in t.error for t in done if t.invalid)
    assert {(t.rep, t.task, t.arm) for t in done} == {(r, k, a) for r in (1, 2) for k in ("t1", "t2") for a in ("A", "B", "A~aa")}
    only = run_experiment(fake_experiment()[0], Context(), 1, arms=["A"], only=["t2"])
    assert [(t.arm, t.task) for t in only] == [("A", "t2")]


def test_the_report_labels_the_power_limit_states_the_rule_and_the_gap_and_the_json_parses():
    exp, _ = fake_experiment()
    done = run_experiment(exp, Context(power_limit_w=400.0), 5)
    text = render_report(exp, Context(power_limit_w=400.0), done, 5)
    assert "**GPU power limit**: 400 W" in text and "Rule, written before the run: x" in text and "A/A: A " in text and "`A~aa`" in text
    assert "Warning" not in text
    assert "Warning" in render_report(exp, Context(power_limit_w=400.0), done, 2)                       # k below 5
    assert "NOT RECORDED" in render_report(exp, Context(), done, 5)
    plumbing = render_report(exp, Context(plumbing=True), done, 5)
    assert "Plumbing check" in plumbing and "not measurements of the model" in plumbing
    data = json.loads(results_json(exp, Context(power_limit_w=400.0), done, 5))
    assert data["experiment"] == "ex" and data["gpu_power_limit_w"] == 400.0 and len(data["trials"]) == len(done) and data["verdicts"][0]["challenger"] == "B"


def test_the_summary_lists_tasks_that_pass_sometimes_as_divergent():
    ts = [CaseTrial("A", "t1", 1, success=True), CaseTrial("A", "t1", 2, success=False), CaseTrial("A", "t2", 1, success=True), CaseTrial("A", "t2", 2, success=True)]
    s = arm_summary(ts, ("success",))
    assert s["divergent"] == ["t1"] and s["pass_pow_k"] == 1 and s["pass_at_k"] == 2


def test_every_experiment_loads_with_its_baseline_control_rule_and_a_question():
    assert set(cases.MODULES) == {"e1", "e2", "e3", "e4", "e5", "e6", "e7", "e8", "e9", "e10", "e11", "e12", "e13"}
    for key in cases.MODULES:
        exp = cases.load(key)
        names = [a.name for a in exp.arms]
        assert exp.id == key and exp.question.endswith("?") and exp.rule.text
        assert exp.rule.baseline in names and exp.rule.control in names and all(c in names for c in exp.rule.challengers)
        assert len(exp.tasks()) >= 1 and len({exp.task_id(t) for t in exp.tasks()}) == len(exp.tasks())
        assert all(a.flags for a in exp.arms), "every arm says the flag or setting that selects it"
    assert [cases.load("e3").needs_model] == [False]


# -- the fixtures ------------------------------------------------------------------------------------------------------

def good_and_bad(files, checks, solution):
    out = []
    for label, sol in (("untouched", {}), ("solved", solution)):
        with tempfile.TemporaryDirectory() as d:
            task = Task(id="t", category="c", prompt="p", files=dict(files), checks=list(checks))
            ws = make_workspace(task, Path(d) / "ws")
            apply_solution(ws, sol)
            out.append((label, run_checks(task, ws, "", [])))
    return out


def test_e1_goals_fail_on_the_untouched_repository_and_pass_with_their_reference_solution():
    goals = e1_target.goals()
    assert len(goals) == 8 and len({g["id"] for g in goals}) == 8
    for g in goals:
        untouched, solved = good_and_bad({**shop_repo.REPO, **g["files"]}, g["checks"], g["solution"])
        assert untouched[1], g["id"]
        assert solved[1] == [], (g["id"], solved)


def test_e1_scores_the_share_of_checks_met_on_a_real_workspace(tmp_path):
    g = next(x for x in e1_target.goals() if x["id"] == "g1_clamp")
    ws = write_files(tmp_path / "ws", {**shop_repo.REPO, **g["files"]})
    ok, fraction, failed = e1_target.score(g, ws)
    assert not ok and 0 < fraction < 1 and failed
    apply_solution(ws, g["solution"])
    assert e1_target.score(g, ws) == (True, 1.0, [])


def test_e2_tasks_start_failing_pass_with_the_solution_and_half_are_traps():
    tasks = e2_gate.tasks()
    assert len(tasks) == 8 and sum(t["trap"] for t in tasks) == 4
    for raw in tasks:
        untouched, solved = good_and_bad(raw["files"], raw["checks"], raw["solution"])
        assert untouched[1] and solved[1] == [], raw["id"]
        assert raw["verify"] == [gate_tasks.VERIFY_CMD]


def test_e2_a_fix_that_edits_the_test_does_not_count():
    raw = next(t for t in e2_gate.tasks() if t["id"] == "t1_window_off_by_one")
    cheat = {"tests/test_seq.py": "import unittest\n\n\nclass SeqTest(unittest.TestCase):\n    def test_windows(self):\n        self.assertTrue(True)\n"}
    assert good_and_bad(raw["files"], raw["checks"], cheat)[1][1] != []


def test_e3_scenarios_cover_transient_deterministic_and_overflow():
    ids = {s["id"] for s in error_scenarios.scenarios()}
    assert ids >= {"transient_429_500", "transient_timeout", "deterministic_400", "deterministic_401", "overflow_forever", "overflow_twice", "loading_400"}
    for s in error_scenarios.scenarios():
        assert s["ideal"] in ("answer", "visible_error") and s["steps"]
        assert (s["ideal"] == "visible_error") == s["forever"] or s["id"].startswith("overflow")
        assert s["ideal"] == "answer" or s["forever"]


def test_e4_has_twenty_prompts_in_six_kinds_and_every_reference_passes():
    rows = e4_intake.prompts()
    assert len(rows) == 20 and {r["category"] for r in rows} == {"plain", "dated", "ambiguous", "risky", "long_list", "injection"}
    assert sum(1 for r in rows if r["should_ask"]) == 7 and sum(1 for r in rows if r["expected_date"]) == 4
    assert all(r["reply"] for r in rows) and all(r["should_ask"] for r in rows if r["category"] in ("ambiguous", "risky"))
    assert not any(r["should_ask"] for r in rows if r["category"] in ("plain", "dated", "long_list", "injection"))
    for r in rows:
        untouched, solved = good_and_bad(r["files"], r["checks"], r["solution"])
        assert untouched[1] and solved[1] == [], r["id"]


def test_e8_has_twenty_nine_requests_in_nine_kinds_each_with_something_to_keep_and_every_reference_passes():
    assert optimize_prompts.prompts() is e8_optimize.prompts() or optimize_prompts.prompts() == e8_optimize.prompts()
    rows = e8_optimize.prompts()
    assert len(rows) == 29 and len({r["id"] for r in rows}) == 29
    assert {r["category"] for r in rows} == {"terse", "numbers", "quoted", "code", "paths", "italian", "vague", "long", "mixed"}
    kinds_of = {c: [r for r in rows if r["category"] == c] for c in ("vague", "long", "mixed")}
    assert [len(v) for v in kinds_of.values()] == [3, 4, 2]
    assert all(len(r["text"].split()) <= 12 and "\n" not in r["text"] for r in kinds_of["vague"])              # one line
    assert all(150 <= len(r["text"].split()) <= 400 and len(r["text"]) <= opt.MAX_INPUT_CHARS for r in kinds_of["long"])
    assert sum(1 for r in kinds_of["long"] if "```" in r["text"]) >= 1 and sum(1 for r in kinds_of["long"] if "https://" in r["text"]) >= 3
    assert any("1. " in r["text"] for r in kinds_of["long"]) and any(r["text"].startswith("Ciao") for r in kinds_of["long"])
    for r in rows:
        assert len(r["text"].split()) >= opt.MIN_WORDS and opt.skip_reason(r["text"]) == "", r["id"]            # none is left alone by the guards
        assert opt.protected(r["text"]), r["id"]                                                              # and each has something a rewrite must keep
        untouched, solved = good_and_bad(r["files"], r["checks"], r["solution"])
        assert untouched[1] and solved[1] == [], r["id"]
    kinds = {k for r in rows for k, _ in opt.protected(r["text"])}
    assert {"file", "number", "quote", "code", "path", "link"} <= kinds


def test_e8_scores_the_task_and_what_the_model_read_against_what_the_person_wrote():
    row = next(r for r in e8_optimize.prompts() if r["id"] == "num1")
    kept = e8_optimize.score_row(row, "Create the file port.txt and write the number 8080 in it, nothing else.", "sent", "", [])
    assert kept == {"success": 1.0, "protected_kept": 1.0, "good": 1.0, "optimized": 1.0, "rejected": 0.0, "lost": 0.0}
    assert e8_optimize.score_row(row, row["text"], "rejected", "lost", [])["lost"] == 1.0 and e8_optimize.score_row(row, row["text"], "skipped", "failed", [])["optimized"] == 0.0
    dropped = e8_optimize.score_row(row, "Create the file port.txt with the port number.", "sent", "", [])
    assert dropped["protected_kept"] == 0.0 and dropped["good"] == 0.0 and dropped["success"] == 1.0
    assert e8_optimize.score_row(row, row["text"], "skipped", "", ["port.txt is wrong"])["good"] == 0.0


def test_e4_dates_are_the_days_the_fixed_clock_gives():
    from datetime import timedelta

    from harness.eval.cases.agentrun import FIXED_NOW
    assert FIXED_NOW.strftime("%A") == "Friday"
    want = {"date1": 1, "date2": 2, "date3": -1, "date4": 3}
    for r in e4_intake.prompts():
        if r["id"] in want:
            assert r["expected_date"] == (FIXED_NOW + timedelta(days=want[r["id"]])).strftime("%Y-%m-%d")


def test_e4_the_rules_arm_writes_the_dates_the_row_expects():
    now = e4_intake.prompts()
    for r in now:
        if r["expected_date"] and r["id"] != "date4":                    # "next Monday" is not one of the words the rules know
            out = it.Intake("rules").run(r["text"], it.Context(now=__import__("harness.eval.cases.agentrun", fromlist=["x"]).FIXED_NOW))
            assert r["expected_date"] in out.note, r["id"]


def test_e5_the_repository_is_generated_the_same_every_time_and_outgrows_the_window():
    a, b = long_turn_repo.build(60, 7), long_turn_repo.build(60, 7)
    assert a.files == b.files and a.functions == b.functions and len(a.functions) == 60 and 30 < len(a.typo_modules) < 60
    assert long_turn_repo.build(45, 11).files != a.files
    assert all(long_turn_repo.TYPO in a.files[p] for p in a.frozen) and len(a.frozen) == 5
    text = a.files["pkg/mod_000.py"]
    assert text.count("\ndef ") == a.functions["mod_000"] or text.startswith("def ")
    assert sum(len(v) for v in a.files.values()) / 3.5 > 16384                           # more than the default window holds
    assert "R1." in long_turn_repo.request(60) and long_turn_repo.FINAL_LINE in long_turn_repo.request(60)


def perfect(repo):
    files = dict(repo.files)
    ledger = ["# Ledger", ""]
    order = []
    for name in sorted(repo.functions):
        text = files[f"pkg/{name}.py"]
        if name in repo.typo_modules:
            files[f"pkg/{name}.py"] = text.replace(long_turn_repo.TYPO, long_turn_repo.FIXED) + long_turn_repo.TRAILER + "\n"
            order.append(name)
        ledger.append(f"{name}: {repo.functions[name]} functions")
    files["LEDGER.md"] = "\n".join(ledger) + "\n"
    return files, order, "All done.\n" + long_turn_repo.FINAL_LINE


def test_e5_scores_a_perfect_result_and_each_way_of_failing():
    repo = long_turn_repo.build(60, 7)
    files, order, answer = perfect(repo)
    s = e5_compaction.score(repo, files, answer, order)
    assert s["success"] == 1.0 and s["rule_obeyed"] == 1.0 and s["ledger_ok"] == 1.0 and s["typos_left"] == 0 and e5_compaction.notes_of(s) == []
    left = dict(files)
    left[f"pkg/{sorted(repo.typo_modules)[0]}.py"] += f"# we {long_turn_repo.TYPO}\n"
    assert e5_compaction.score(repo, left, answer, order)["success"] == 0.0
    frozen = dict(files)
    frozen[repo.frozen[0]] = frozen[repo.frozen[0]].replace(long_turn_repo.TYPO, long_turn_repo.FIXED)
    s = e5_compaction.score(repo, frozen, answer, order)
    assert s["success"] == 0.0 and s["frozen_intact"] == 0.0 and "frozen" in " ".join(e5_compaction.notes_of(s))
    thin = dict(files)
    thin["LEDGER.md"] = "# Ledger\n"
    assert e5_compaction.score(repo, thin, answer, order)["success"] == 0.0
    forgot = {k: (v[: -len(long_turn_repo.TRAILER) - 1] if k.startswith("pkg/") and v.endswith(long_turn_repo.TRAILER + "\n") and k.endswith(f"{order[-1]}.py") else v) for k, v in files.items()}
    s = e5_compaction.score(repo, forgot, answer, order)
    assert s["success"] == 1.0 and s["rule_obeyed"] == 0.0 and s["trailers_late"] < 1.0               # the work is done, the early rule was dropped
    s = e5_compaction.score(repo, files, "All done.", order)
    assert s["success"] == 1.0 and s["rule_obeyed"] == 0.0 and s["final_line"] == 0.0


def test_e6_tasks_have_the_sizes_the_owner_asked_for_and_the_protected_content():
    tasks = {t["id"]: t for t in e6_longform.tasks()}
    assert [tasks[k]["words"] for k in ("write3000", "write6000", "write10000")] == [3000, 6000, 10000]
    assert all(p in tasks["translate3600"]["source"] for p in longform_texts.PROTECTED)
    assert len(tasks["analysis"]["incidents"]) == 12 and all(i in tasks["analysis"]["source"] for i in tasks["analysis"]["incidents"])
    assert tasks["translate3600"]["outline"] == [] and all(tasks[k]["outline"] for k in ("write3000", "analysis"))
    assert sum(s["target_words"] for s in tasks["write10000"]["outline"]) == 10000
    assert longform_texts.manual(3600) == longform_texts.manual(3600)


def text_of(words, seed=0):
    import random
    rng = random.Random(seed)
    vocab = [f"w{i}x" for i in range(400)]
    paras = []
    n = 0
    while n < words:
        para = " ".join(rng.choice(vocab) for _ in range(60)) + "."
        paras.append(para)
        n += 60
    return paras


def test_e6_scores_a_clean_text_and_each_fault():
    task = {"id": "w", "kind": "write", "words": 600, "language": "en"}
    paras = text_of(600)
    parts = ["\n\n".join(paras[:5]), "\n\n".join(paras[5:])]
    good = e6_longform.score_text(task, "\n\n".join(parts), parts, [1.0, 1.0])
    assert good["success"] == 1.0 and good["seam_issues"] == 0 and good["repeat_paragraphs"] == 0
    short = e6_longform.score_text(task, parts[0], [parts[0]], [])
    assert short["success"] == 0.0 and short["delivered_ratio"] < 0.85
    twice = "\n\n".join(parts + [paras[1]])
    assert e6_longform.score_text(task, twice, parts + [paras[1]], [])["repeat_paragraphs"] == 1.0
    cut = [parts[0] + " and then the", parts[1]]
    assert any("middle of a sentence" in s for s in e6_longform.seams(cut))
    assert e6_longform.seams(["The end of one part is here now and ends well. In conclusion, all is well.", "More text follows after."])[0].startswith("part 1 closes")
    assert e6_longform.seams(["Some text that ends with alpha beta gamma delta epsilon zeta", "alpha beta gamma delta epsilon zeta and goes on from there."])
    assert e6_longform.seams(["# Part 2\n\nSome words here.", "# Part 2\n\nMore words"]) and e6_longform.seams(["Fine text ends here.", "Next text starts here."]) == []


def test_e6_scores_protected_content_language_and_coverage():
    tasks = {t["id"]: t for t in e6_longform.tasks()}
    tr = dict(tasks["translate3600"], words=300)
    import random
    rng = random.Random(4)
    body = " ".join(rng.choice(lf.STOPWORDS["it"]) if rng.random() < 0.5 else f"termine{rng.randint(0, 900)}" for _ in range(320))
    intact = body + "\n\n" + "\n\n".join(longform_texts.PROTECTED)
    s = e6_longform.score_text(tr, intact, [intact], [])
    assert s["protected_intact"] == 1.0 and s["language_ok"] == 1.0 and s["success"] == 1.0
    broken = intact.replace(longform_texts.CODE, "```python\ndef backoff(attempt):\n    pass\n```")
    s = e6_longform.score_text(tr, broken, [broken], [])
    assert s["protected_intact"] < 1.0 and s["success"] == 0.0
    an = dict(tasks["analysis"], words=300)
    ids = " ".join(an["incidents"])
    base = " ".join(f"word{i}" for i in range(320))
    assert e6_longform.score_text(an, base + " " + ids, [base + " " + ids], [])["coverage"] == 1.0
    missing = e6_longform.score_text(an, base + " " + " ".join(an["incidents"][:6]), [base], [])
    assert missing["coverage"] == 0.5 and missing["success"] == 0.0


def test_e6_arms_set_the_segment_size_the_plan_and_the_mode():
    task = next(t for t in e6_longform.tasks() if t["id"] == "write6000")
    arms = {a.name: a for a in e6_longform.ARMS}
    assert [lf.Spec.from_request({"segment_words": arms[n].settings["segment_words"], "task": "x"}, "").segment_words for n in ("A", "B", "C")] == [2000, 1000, 2500]
    plan = e6_longform.spec_for(arms["D"], task)
    assert plan.outline and plan.segment_words == 2000 and lf.chunk_count(6000, 2000) == 3
    assert not e6_longform.spec_for(arms["A"], task).outline
    tr = e6_longform.spec_for(arms["B"], next(t for t in e6_longform.tasks() if t["kind"] == "translate"))
    assert tr.mode == "translate" and tr.language == "it" and tr.segment_words == 1000
    assert arms["E"].settings["how"] == "single" and arms["A~aa"].settings == arms["A"].settings


def test_e7_survival_is_zero_for_the_worst_case_one_for_the_best_and_unknown_when_they_match():
    mk = lambda arm, v: [CaseTrial(arm, "t", i, success=True, metrics={"cached_share": x}) for i, x in enumerate(v)]
    g = {"A": mk("A", [0.95, 0.97]), "A~aa": mk("A~aa", [0.96]), "B": mk("B", [0.9]), "G": mk("G", [0.05, 0.03]), "C": mk("C", [0.5])}
    s = e7_cache.survival(g)
    assert s["G"] == 0.0 and s["B"] > 0.9 and 0.4 < s["C"] < 0.6
    flat = {"A": mk("A", [0.5]), "G": mk("G", [0.5])}
    assert all(v is None for v in e7_cache.survival(flat).values())
    text = "\n".join(e7_cache.extra_report([t for ts in g.values() for t in ts]))
    assert "Survival of the prefix cache" in text and "never-seen" in text


# -- the arms are wired to settings that exist ------------------------------------------------------------------------

def test_e1_arms_are_target_loop_options():
    first = {a.name: tg.clean_options(a.settings)["first_message"] for a in e1_target.ARMS}
    assert first == {"A": "joined", "B": "separate", "A~aa": "joined"}
    assert tg.clean_options({})["first_message"] == "joined" and tg.clean_options({"first_message": "nope"})["first_message"] == "joined"


def test_e2_arms_are_agent_config_fields():
    fields = {f.name for f in dataclasses.fields(AgentConfig)}
    for arm in e2_gate.ARMS:
        assert {k for k in arm.settings if k != "use_task_command"} <= fields
    assert {a.name: a.settings["verify_gate"] for a in e2_gate.ARMS} == {"A": "off", "B": "always", "A~aa": "off"}


def test_e3_e4_e5_arms_are_the_flags_the_session_and_the_agent_take():
    from harness.session import parse_args
    assert {a.name: a.settings["classify_errors"] for a in e3_errors.ARMS} == {"A": False, "B": True, "A~aa": False}
    assert parse_args([]).no_error_classifier is False and parse_args(["--no-error-classifier"]).no_error_classifier is True
    assert {a.settings["intake"] for a in e4_intake.ARMS} == set(it.MODES)
    assert all(parse_args(["--intake", a.settings["intake"]]).intake == a.settings["intake"] for a in e4_intake.ARMS)
    fields = {f.name for f in dataclasses.fields(AgentConfig)}
    assert all(set(a.settings) <= fields for a in e5_compaction.ARMS)
    assert {a.name: a.settings["compaction"] for a in e5_compaction.ARMS} == {"A": False, "B": True, "A~aa": False}
    assert {a.name: a.settings["optimize"] for a in e8_optimize.ARMS} == {"A": False, "B": True, "A~aa": False}
    assert all(parse_args(a.flags.split()).optimize_prompt == ("on" if a.settings["optimize"] else "off") for a in e8_optimize.ARMS)


def test_e7_shapes_are_all_known_and_each_cycle_has_text_of_its_own():
    sent = []

    def send(messages, tools):
        sent.append((json.dumps(messages), json.dumps(tools)))
        return e7_cache.Seen(0.1, 0.2, 10, 0, "ok")
    for arm in e7_cache.ARMS:
        e7_cache.run_shape(arm.settings["shape"], send, "salt1", 100)
    with pytest.raises(ValueError):
        e7_cache.run_shape("nonsense", send, "s", 10)
    a, b = [], []
    e7_cache.run_shape("identical", lambda m, t: (a.append(json.dumps(m)), e7_cache.Seen(0, 0, 1, 0, "x"))[1], "one", 100)
    e7_cache.run_shape("identical", lambda m, t: (b.append(json.dumps(m)), e7_cache.Seen(0, 0, 1, 0, "x"))[1], "two", 100)
    assert a[0] == a[1] and b[0] == b[1] and a[0] != b[0]


def test_wasted_turns_counts_turns_that_moved_nothing():
    from harness.agent_loop import AgentTurn, ToolExecutionResult as R
    ok = lambda args: R("read_file", args, "x", decision="executed")
    turns = [AgentTurn(1, "", "", "", [ok({"p": 1})]), AgentTurn(2, "", "", "", [ok({"p": 1})]), AgentTurn(3, "", "", "", [R("edit_file", {}, "no", is_error=True, decision="invalid")]),
             AgentTurn(4, "", "", "", [ok({"p": 2})]), AgentTurn(5, "", "", "done", [])]
    assert wasted_turns(turns) == 2
