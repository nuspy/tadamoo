"""A benchmark run (tadamoo/bench/runner.py): its folder, journal, resume, stop flag and adapters, with the fake adapter. CPU only."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.bench import runner
from tadamoo.bench.runner import AdapterNotWritten, BenchRun, FakeAdapter, TrialResult, get_adapter, read_trials
from tadamoo.eval import resume
from bench_support import MODEL, make_spec


@pytest.fixture(autouse=True)
def fixed_tree(monkeypatch):
    monkeypatch.setattr(resume, "git_state", lambda root: {"commit": "abc123", "dirty": False, "changes": ""})


def new_run(tmp_path, adapter=None, settings=None, spec=None, power=400.0, commit="c0ffee", **kw):
    return BenchRun(tmp_path / "b", spec or make_spec("x"), settings or {"trials": 2}, adapter=adapter or FakeAdapter(), model=MODEL, power_limit_w=power,
                    imported_commit=commit, **kw)


def test_a_new_run_writes_its_folder_and_state(tmp_path):
    run = new_run(tmp_path)
    assert run.dir.parent == tmp_path / "b" / "runs"
    rec = json.loads((run.dir / "settings.json").read_text())
    assert rec["benchmark"] == "fakebench" and rec["benchmark_commit"] == "c0ffee" and rec["power_limit_w"] == 400.0
    assert rec["model"]["quantisation"] == "IQ4_XS" and rec["harness"]["commit"] == "abc123" and rec["provisional"] is False
    assert run.state()["status"] == "queued"


def test_executing_runs_every_trial_and_scores(tmp_path):
    fake = FakeAdapter(outcomes={("t2", 1): {"passed": False, "error": "x"}})
    run = new_run(tmp_path, fake)
    st = run.execute()
    assert st["status"] == "done" and st["done"] == 4 == st["total"] and st["passed"] == 3 and st["failed"] == 1 and st["tokens"] == 400 and st["current"] == ""
    res = json.loads((run.dir / "results.json").read_text())
    assert res["metrics"]["resolution_rate"] == 0.75 and res["provisional"] is False
    lines = (run.dir / "trials.jsonl").read_text().splitlines()
    assert "fingerprint" in json.loads(lines[0]) and len(lines) == 5
    assert set(read_trials(run.dir)) == {("t1", 1), ("t1", 2), ("t2", 1), ("t2", 2)}


def test_trial_files_are_left_in_the_run_folder(tmp_path):
    fake = FakeAdapter(outcomes={("t1", 1): {"passed": False, "events": [{"type": "stop", "reason": "timeout"}], "verifier": "FAILED test_x"}})
    run = new_run(tmp_path, fake, {"trials": 1})
    run.execute()
    res = read_trials(run.dir)[("t1", 1)][1]
    assert (run.dir / res.trajectory_path).is_file() and (run.dir / res.verifier_output_path).read_text() == "FAILED test_x"


def test_resume_reuses_finished_trials(tmp_path):
    first = FakeAdapter()
    run = new_run(tmp_path, first)
    run.execute()
    second = FakeAdapter()
    again = BenchRun(tmp_path / "b", make_spec("x"), {}, adapter=second, run_id=run.run_id)
    assert again.record["settings"] == {"trials": 2} and again.execute()["status"] == "done"
    assert second.ran == []                                         # nothing was run again


def test_a_trial_that_could_not_be_judged_is_redone(tmp_path):
    flaky = FakeAdapter(outcomes={("t1", 1): {"passed": None, "error": "docker daemon not running"}})
    run = new_run(tmp_path, flaky, {"trials": 1})
    st = run.execute()
    assert st["invalid"] == 1 and st["status"] == "done"
    healthy = FakeAdapter()
    BenchRun(tmp_path / "b", make_spec("x"), {}, adapter=healthy, run_id=run.run_id).execute()
    assert healthy.ran == [("t1", 1)]


def test_a_crashing_adapter_is_recorded_not_raised(tmp_path):
    class Crashing(FakeAdapter):
        def run_trial(self, key, ctx):
            raise RuntimeError("boom")
    run = new_run(tmp_path, Crashing(), {"trials": 1})
    st = run.execute()
    assert st["status"] == "done" and st["invalid"] == 2
    assert "adapter crashed: RuntimeError: boom" in read_trials(run.dir)[("t1", 1)][1].error


def test_a_changed_setup_refuses_the_old_trials(tmp_path, monkeypatch):
    run = new_run(tmp_path)
    run.execute()
    record = json.loads((run.dir / "settings.json").read_text())
    record["power_limit_w"] = 300.0                                  # the same folder, another power limit
    (run.dir / "settings.json").write_text(json.dumps(record))
    again = BenchRun(tmp_path / "b", make_spec("x"), {}, adapter=FakeAdapter(), run_id=run.run_id)
    st = again.execute()
    assert st["status"] == "failed" and "power_limit_w" in st["error"] and "fingerprint" in st["error"]
    monkeypatch.setattr(resume, "git_state", lambda root: {"commit": "def456", "dirty": False, "changes": ""})
    record["power_limit_w"] = 400.0
    (run.dir / "settings.json").write_text(json.dumps(record))
    st = BenchRun(tmp_path / "b", make_spec("x"), {}, adapter=FakeAdapter(), run_id=run.run_id).execute()
    assert st["status"] == "failed" and "git.commit" in st["error"]


def test_the_stop_flag_stops_between_trials_and_the_run_resumes(tmp_path):
    holder = {}
    fake = FakeAdapter(on_trial=lambda key: holder["run"].request_stop() if key == ("t1", 2) else None)
    run = new_run(tmp_path, fake)
    holder["run"] = run
    st = run.execute()
    assert st["status"] == "stopped" and st["done"] == 2 and not (run.dir / "results.json").exists()
    rest = FakeAdapter()
    final = BenchRun(tmp_path / "b", make_spec("x"), {}, adapter=rest, run_id=run.run_id).execute()
    assert final["status"] == "done" and rest.ran == [("t2", 1), ("t2", 2)]


def test_the_context_tells_a_trial_to_stop(tmp_path):
    seen = []
    fake = FakeAdapter(on_trial=lambda key: None)
    original = fake.run_trial

    def spy(key, ctx):
        seen.append(ctx.should_stop())
        return original(key, ctx)
    fake.run_trial = spy
    new_run(tmp_path, fake, {"trials": 1}).execute()
    assert seen == [False, False]


def test_a_cloud_judge_benchmark_is_marked_provisional(tmp_path):
    run = new_run(tmp_path, spec=make_spec("x", official_cloud_judge=True))
    run.execute()
    res = json.loads((run.dir / "results.json").read_text())
    assert res["provisional"] is True and "unofficial judge: not comparable with the board" in res["provisional_label"]


def test_adapters_that_are_not_written_say_so():
    with pytest.raises(AdapterNotWritten, match="clawbench is not written yet"):
        get_adapter("clawbench")
    assert get_adapter("x", {"x": FakeAdapter}).id == "fake"


def test_a_plan_that_fails_fails_the_run(tmp_path):
    class NoPlan(FakeAdapter):
        def plan(self, settings, bench_dir):
            raise AdapterNotWritten("The adapter for x is not written yet")
    st = new_run(tmp_path, NoPlan()).execute()
    assert st["status"] == "failed" and "not written yet" in st["error"]


def test_trial_result_round_trips_and_ignores_unknown_fields():
    r = TrialResult(passed=False, score=0.5, seconds=2, tokens=3, error="e", facts={"a": 1})
    assert TrialResult.from_dict({**r.to_dict(), "future": 1}) == r
    assert not TrialResult().valid and TrialResult(passed=False).valid


def test_background_thread_runs_the_same(tmp_path):
    run = new_run(tmp_path)
    run.start_background().join(10)
    assert run.state()["status"] == "done"
