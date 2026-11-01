"""Which of several runs of one benchmark is better (harness/bench/ranking.py compare_runs): each benchmark's own metric and interval, ties reported as ties,
and no winner while the intervals overlap. CPU only."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_support import MODEL, make_spec
from harness.bench import ranking
from harness.bench.runner import BenchRun, FakeAdapter


def tasks(solved, total, trials=1):
    """per_task for `solved` tasks that pass in every trial and the rest that fail in every trial."""
    return {f"t{i:03d}": [i < solved] * trials for i in range(total)}


def tb_run(name, solved, total=89, trials=1, **extra):
    pt = tasks(solved, total, trials)
    flat = [x for v in pt.values() for x in v]
    return {"name": name, "metrics": {"resolution_rate": sum(flat) / len(flat)}, "trials": len(flat), "successes": sum(flat), "per_task": pt, **extra}


def wilson_run(name, successes, n, metric="resolution_rate", **extra):
    return {"name": name, "metrics": {metric: successes / n}, "trials": n, "successes": successes, **extra}


# -- Terminal-Bench: the mean over tasks -------------------------------------------------------------------------------

def test_terminal_bench_names_a_winner_only_when_the_task_intervals_do_not_overlap():
    out = ranking.compare_runs("terminal-bench-2", [tb_run("before", 10), tb_run("after", 40)])
    assert out["winner"] == "after" and out["tie"] == [] and [r["name"] for r in out["ordered"]] == ["after", "before"]
    assert out["ordered"][0]["rank"] == 1 and out["ordered"][1]["rank"] == 2 and out["ordered"][1]["beaten_by"] == ["after"]
    assert out["ordered"][0]["interval"]["basis"] == "tasks" and "clearly ahead" in out["verdict"]
    close = ranking.compare_runs("terminal-bench-2", [tb_run("a", 20), tb_run("b", 26)])
    assert close["winner"] is None and close["tie"] == ["b", "a"] and close["ordered"][0]["tied_with"] == ["a"]
    assert "tied" in close["verdict"]


def test_five_trials_of_each_task_do_not_shrink_the_interval():
    """Trials of one task are not independent: with k trials the interval still has only the tasks as its sample."""
    one = ranking.compare_runs("terminal-bench-2", [tb_run("a", 20)] * 1 + [tb_run("b", 26)])
    five = ranking.compare_runs("terminal-bench-2", [tb_run("a", 20, trials=5), tb_run("b", 26, trials=5)])
    assert five["winner"] is None
    width = lambda out: out["ordered"][0]["interval"]["high"] - out["ordered"][0]["interval"]["low"]
    assert width(five) == pytest.approx(width(one), rel=0.01) and five["ordered"][0]["interval"]["n"] == 89


def test_equal_runs_are_a_tie_and_three_runs_can_share_the_lead():
    same = ranking.compare_runs("terminal-bench-2", [tb_run("x", 30), tb_run("y", 30)])
    assert same["winner"] is None and sorted(same["tie"]) == ["x", "y"]
    three = ranking.compare_runs("terminal-bench-2", [tb_run("low", 5), tb_run("top", 45), tb_run("alike", 41)])
    assert three["winner"] is None and sorted(three["tie"]) == ["alike", "top"]
    by = {r["name"]: r for r in three["ordered"]}
    assert by["low"]["rank"] == 3 and by["top"]["rank"] == 1 and by["alike"]["rank"] == 1 and sorted(by["low"]["beaten_by"]) == ["alike", "top"]
    lead = ranking.compare_runs("terminal-bench-2", [tb_run("low", 5), tb_run("mid", 8), tb_run("top", 60)])
    assert lead["winner"] == "top"


# -- SWE-bench Verified: Wilson over instances -------------------------------------------------------------------------

def test_swe_bench_uses_wilson_intervals_of_the_instances():
    clear = ranking.compare_runs("swe-bench-verified", [wilson_run("baseline", 45, 100), wilson_run("harness", 75, 100)])
    assert clear["winner"] == "harness" and clear["metric"] == "resolution_rate" and clear["interval"] == "wilson_trials"
    overlap = ranking.compare_runs("swe-bench-verified", [wilson_run("baseline", 45, 100), wilson_run("harness", 60, 100)])
    assert overlap["winner"] is None and sorted(overlap["tie"]) == ["baseline", "harness"]
    small = ranking.compare_runs("swe-bench-verified", [wilson_run("baseline", 1, 2), wilson_run("harness", 2, 2)])
    assert small["winner"] is None                                  # two instances can never settle it


def test_a_paired_test_is_shown_beside_but_does_not_decide():
    a = {"name": "a", "metrics": {"resolution_rate": 0.6}, "trials": 20, "successes": 12, "per_task": {f"i{k}": [k < 12] for k in range(20)}}
    b = {"name": "b", "metrics": {"resolution_rate": 0.4}, "trials": 20, "successes": 8, "per_task": {f"i{k}": [k < 8] for k in range(20)}}
    out = ranking.compare_runs("swe-bench-verified", [a, b])
    assert out["winner"] is None and out["paired"]["mcnemar_p"] == pytest.approx(2 * (1 / 16))
    assert ranking.mcnemar_p({"a": [True]}, {"a": [True]}) == 1.0
    assert ranking.mcnemar_p({f"i{k}": [True] for k in range(8)}, {f"i{k}": [False] for k in range(8)}) == pytest.approx(2 / 256)
    assert ranking.mcnemar_p({"a": [True]}, {"b": [True]}) is None


# -- other benchmarks --------------------------------------------------------------------------------------------------

def test_each_benchmark_is_compared_on_its_own_metric():
    assert ranking.compare_runs("bfcl-v4", [wilson_run("a", 50, 200, "accuracy"), wilson_run("b", 150, 200, "accuracy")])["winner"] == "b"
    assert ranking.compare_runs("clawbench", [wilson_run("a", 2, 20, "reward_rate"), wilson_run("b", 3, 20, "reward_rate")])["winner"] is None
    wrong = ranking.compare_runs("bfcl-v4", [wilson_run("a", 50, 200, "resolution_rate"), wilson_run("b", 150, 200, "accuracy")])
    assert wrong["unranked"] == [{"name": "a", "why": "the run has no 'accuracy' metric"}] and wrong["winner"] is None


def test_tau3_resamples_the_tasks_and_is_repeatable():
    def run(name, solved, total=30, k=3):
        pt = {f"t{i}": [i < solved] * k for i in range(total)}
        return {"name": name, "metrics": {"pass_hat_k": solved / total}, "per_task": pt, "k": k}
    out = ranking.compare_runs("tau3-bench", [run("a", 5), run("b", 24)])
    assert out["winner"] == "b" and out["interval"] == "bootstrap_tasks"
    assert ranking.compare_runs("tau3-bench", [run("a", 5), run("b", 24)]) == out
    assert ranking.compare_runs("tau3-bench", [run("a", 14), run("b", 17)])["winner"] is None
    assert ranking._pass_hat_k([True, True, False, True], 2) == pytest.approx(3 / 6)
    no_tasks = ranking.compare_runs("tau3-bench", [{"name": "a", "metrics": {"pass_hat_k": 0.1}}, {"name": "b", "metrics": {"pass_hat_k": 0.9}}])
    assert no_tasks["winner"] is None and any("no interval" in w for w in no_tasks["warnings"])


# -- when a comparison is not like for like ----------------------------------------------------------------------------

def test_different_subsets_or_tasks_never_produce_a_winner():
    out = ranking.compare_runs("swe-bench-verified", [wilson_run("a", 10, 100, subset="dev"), wilson_run("b", 90, 100, subset="random100")])
    assert out["winner"] is None and any("different subsets" in w for w in out["warnings"])
    other = ranking.compare_runs("terminal-bench-2", [tb_run("a", 5, total=89), tb_run("b", 40, total=60)])
    assert other["winner"] is None and any("same tasks" in w for w in other["warnings"])


def test_a_provisional_score_is_flagged_and_a_single_run_has_no_winner():
    out = ranking.compare_runs("clawbench", [wilson_run("a", 2, 20, "reward_rate", provisional=True), wilson_run("b", 19, 20, "reward_rate")])
    assert out["winner"] == "b" and any("not comparable with the board" in w for w in out["warnings"])
    alone = ranking.compare_runs("terminal-bench-2", [tb_run("only", 30)])
    assert alone["winner"] is None and alone["tie"] == []
    assert ranking.compare_runs("terminal-bench-2", [])["verdict"] == "There are no runs to compare."


def test_an_unknown_benchmark_is_an_error_unless_the_spec_names_its_metric():
    with pytest.raises(KeyError, match="no comparison rule"):
        ranking.compare_runs("not-a-bench", [])
    spec = make_spec("unused", id="local-one", primary_metric="score")
    out = ranking.compare_runs(spec, [wilson_run("a", 5, 100, "score"), wilson_run("b", 80, 100, "score")])
    assert out["winner"] == "b" and out["metric"] == "score"


def test_run_summary_reads_a_finished_run_folder(tmp_path):
    spec = make_spec("unused", id="swe-bench-verified")
    adapter = FakeAdapter(id="swe-bench-verified", tasks=["a", "b", "c"], outcomes={("b", 1): {"passed": False}, ("c", 1): {"passed": None, "error": "docker"}})
    run = BenchRun(tmp_path, spec, {"subset": "dev-baseline", "trials": 1}, adapter=adapter, model=MODEL, power_limit_w=300.0, root=tmp_path)
    run.execute()
    s = ranking.run_summary(run.dir)
    assert s["per_task"] == {"a": [True], "b": [False]} and (s["trials"], s["successes"]) == (2, 1)       # the unjudged trial is not a sample
    assert s["subset"] == "dev" and s["arm"] == "baseline" and s["name"].endswith("(baseline)") and s["metrics"]["resolution_rate"] == 0.5 and not s["provisional"]
    assert ranking.compare_runs("swe-bench-verified", [s, {**s, "name": "other", "arm": ""}])["winner"] is None
