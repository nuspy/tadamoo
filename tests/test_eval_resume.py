"""Resuming an interrupted run (harness/eval/resume.py): the same fingerprint reuses finished valid trials, another refuses, invalid trials are redone. CPU only, no model."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.eval import EvalRunner, Task, resume
from harness.eval.cases.core import Arm, CaseTrial, Context, Experiment, Rule, run_experiment, run_fingerprint
from harness.eval.resume import FILE, Journal, ResumeMismatch, differences, git_state as real_git_state, make_fingerprint
from harness.eval.runner import Trial, trial_is_valid
from test_agent_loop import ScriptedClient, say


@pytest.fixture(autouse=True)
def fixed_tree(monkeypatch):
    monkeypatch.setattr(resume, "git_state", lambda root: {"commit": "abc123", "dirty": False, "changes": ""})


def fingerprint(**over):
    base = dict(experiment="e0", k=2, arms=[{"name": "A", "settings": {"x": 1}}], tasks=["t1", "t2"], sampling={"temperature": 0.6, "seed_base": 1000},
                environment={"Model": "m", "Server setup": "parallel=1"}, model_file=None)
    base.update(over)
    return make_fingerprint(**base)


# -- the fingerprint ------------------------------------------------------------------------------------------------

def test_equal_inputs_give_equal_fingerprints_and_a_difference_names_the_part():
    assert fingerprint() == fingerprint() and differences(fingerprint(), fingerprint()) == []
    assert differences(fingerprint(), fingerprint(k=3)) == ["k: 2 -> 3"]
    assert differences(fingerprint(), fingerprint(sampling={"temperature": 0.7, "seed_base": 1000})) == ["sampling.temperature: 0.6 -> 0.7"]
    assert any(d.startswith("arms") for d in differences(fingerprint(), fingerprint(arms=[{"name": "A", "settings": {"x": 2}}])))
    assert any(d.startswith("tasks") for d in differences(fingerprint(), fingerprint(tasks=["t1"])))
    assert any(d.startswith("environment.Server setup") for d in differences(fingerprint(), fingerprint(environment={"Model": "m", "Server setup": "parallel=2"})))


def test_the_commit_the_uncommitted_changes_and_the_model_file_are_part_of_it(monkeypatch, tmp_path):
    base = fingerprint()
    monkeypatch.setattr(resume, "git_state", lambda root: {"commit": "def456", "dirty": False, "changes": ""})
    assert differences(base, fingerprint()) == ["git.commit: \"abc123\" -> \"def456\""]
    monkeypatch.setattr(resume, "git_state", lambda root: {"commit": "abc123", "dirty": True, "changes": "1f2e3d"})
    assert {d.split(":")[0] for d in differences(base, fingerprint())} == {"git.dirty", "git.changes"}
    monkeypatch.setattr(resume, "git_state", lambda root: {"commit": "abc123", "dirty": False, "changes": ""})
    model = tmp_path / "m.basalt"
    model.write_bytes(b"x" * 10)
    first = fingerprint(model_file=str(model))
    model.write_bytes(b"x" * 11)
    assert differences(first, fingerprint(model_file=str(model))) == ["model_file.size: 10 -> 11"]


def test_the_real_git_state_reads_this_repository():
    state = real_git_state(Path(__file__).resolve().parents[1])
    assert len(state["commit"]) == 40 and isinstance(state["dirty"], bool) and (state["changes"] != "") == state["dirty"]

# -- the journal ----------------------------------------------------------------------------------------------------

def test_a_valid_trial_is_reused_and_an_invalid_one_is_redone(tmp_path):
    j = Journal(tmp_path, fingerprint())
    j.add(("A", "t1", 1), {"v": 1}, True)
    j.add(("A", "t1", 2), {"v": 2}, False)
    again = Journal(tmp_path, fingerprint())
    assert again.get(("A", "t1", 1)) == {"v": 1} and again.get(("A", "t1", 2)) is None and again.get(("A", "t2", 1)) is None
    assert again.reused == 1
    again.add(("A", "t1", 2), {"v": 3}, True)                         # the redo replaces the invalid line
    assert Journal(tmp_path, fingerprint()).get(("A", "t1", 2)) == {"v": 3}


def test_another_fingerprint_refuses_and_leaves_the_journal_alone(tmp_path):
    Journal(tmp_path, fingerprint()).add(("A", "t1", 1), {"v": 1}, True)
    before = (tmp_path / FILE).read_bytes()
    with pytest.raises(ResumeMismatch) as err:
        Journal(tmp_path, fingerprint(k=5, sampling={"temperature": 0.9, "seed_base": 1000}))
    assert err.value.parts == ["k: 2 -> 5", "sampling.temperature: 0.6 -> 0.9"] and "k: 2 -> 5" in str(err.value)
    assert (tmp_path / FILE).read_bytes() == before


def test_a_torn_last_line_is_dropped_and_the_journal_stays_usable(tmp_path):
    j = Journal(tmp_path, fingerprint())
    j.add(("A", "t1", 1), {"v": 1}, True)
    j.add(("A", "t1", 2), {"v": 2}, True)
    with open(tmp_path / FILE, "ab") as f:
        f.write(b'{"key": ["A", "t2", 1], "valid": tr')              # the crash cut this write short
    again = Journal(tmp_path, fingerprint())
    assert again.get(("A", "t1", 1)) == {"v": 1} and again.get(("A", "t1", 2)) == {"v": 2} and again.get(("A", "t2", 1)) is None
    again.add(("A", "t2", 1), {"v": 9}, True)
    lines = (tmp_path / FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4 and all(json.loads(line) for line in lines)
    assert Journal(tmp_path, fingerprint()).get(("A", "t2", 1)) == {"v": 9}


def test_a_torn_header_starts_the_run_afresh_and_a_damaged_middle_line_is_an_error(tmp_path):
    (tmp_path / FILE).write_bytes(b'{"fingerprint": {"k"')
    assert Journal(tmp_path, fingerprint()).records == {}
    Journal(tmp_path, fingerprint()).add(("A", "t1", 1), {"v": 1}, True)
    with open(tmp_path / FILE, "ab") as f:
        f.write(b"garbage\n" + json.dumps({"key": ["A", "t1", 2], "valid": True, "trial": {}}).encode() + b"\n")
    with pytest.raises(ValueError, match="damaged"):
        Journal(tmp_path, fingerprint())


# -- the case experiments -------------------------------------------------------------------------------------------

def experiment(calls, fail_at=None, invalid_once=()):
    def run_trial(arm, task, rep, ctx):
        calls.append((arm.name, task, rep))
        if fail_at and len(calls) == fail_at:
            raise KeyboardInterrupt                                  # the run is killed; no Exception handler may swallow it
        bad = (arm.name, task, rep) in invalid_once and calls.count((arm.name, task, rep)) == 1
        return CaseTrial(arm.name, task, rep, success=rep % 2 == 1, metrics={"seconds": 1.0}, trace=[{"name": "t", "args": "", "key": "k"}], invalid=bad,
                         error="connection closed" if bad else "")
    return Experiment(id="e0", title="t", question="q", arms=[Arm("A", "a", {"x": 1}), Arm("A~aa", "a", {"x": 1}, copy_of="A")], rule=Rule("success", "higher", 0.1, "x", challengers=()),
                      columns=("success",), tasks=lambda: ["t1", "t2"], run_trial=run_trial, task_id=lambda t: t)


def fp(exp, ctx, k=2):
    return run_fingerprint(exp, ctx, k)


def test_an_interrupted_run_continues_with_the_finished_trials_and_matches_an_uninterrupted_one(tmp_path):
    ctx = Context(plumbing=True)
    clean = run_experiment(experiment([]), ctx, 2)
    first_calls = []
    exp = experiment(first_calls, fail_at=6)
    with pytest.raises(KeyboardInterrupt):
        run_experiment(exp, ctx, 2, journal=Journal(tmp_path, fp(exp, ctx)))
    assert len(first_calls) == 6 and len((tmp_path / FILE).read_text(encoding="utf-8").splitlines()) == 1 + 5          # the trial in flight is the only one lost
    second_calls = []
    exp2 = experiment(second_calls)
    journal = Journal(tmp_path, fp(exp2, ctx))
    resumed = run_experiment(exp2, ctx, 2, journal=journal)
    assert len(second_calls) == 8 - 5 and journal.reused == 5 and resumed == clean
    assert set(second_calls).isdisjoint(first_calls[:5])


def test_invalid_trials_are_redone_on_resume(tmp_path):
    ctx = Context(plumbing=True)
    calls = []
    exp = experiment(calls, invalid_once={("A", "t2", 1)})
    first = run_experiment(exp, ctx, 1, journal=Journal(tmp_path, fp(exp, ctx, 1)))
    assert [t.invalid for t in first].count(True) == 1
    again = []
    exp2 = experiment(again)
    done = run_experiment(exp2, ctx, 1, journal=Journal(tmp_path, fp(exp2, ctx, 1)))
    assert again == [("A", "t2", 1)] and not any(t.invalid for t in done)


def test_a_changed_arm_or_k_or_setting_refuses_to_reuse(tmp_path):
    ctx = Context(plumbing=True)
    exp = experiment([])
    run_experiment(exp, ctx, 2, journal=Journal(tmp_path, fp(exp, ctx)))
    with pytest.raises(ResumeMismatch, match="k: 2 -> 3"):
        Journal(tmp_path, fp(exp, ctx, 3))
    with pytest.raises(ResumeMismatch, match="sampling.temperature"):
        Journal(tmp_path, fp(exp, Context(plumbing=True, temperature=0.2)))
    changed = experiment([])
    changed.arms[0] = Arm("A", "a", {"x": 2})
    with pytest.raises(ResumeMismatch, match="arms"):
        Journal(tmp_path, fp(changed, ctx))
    assert run_fingerprint(exp, ctx, 2, arms=["A"]) != fp(exp, ctx)             # a different choice of arms is another run


# -- the generic evaluation runner ----------------------------------------------------------------------------------

def task():
    return Task(id="say", category="c", prompt="Say hi", checks=[{"type": "answer_contains", "text": "hi"}])


def test_the_evaluation_runner_reuses_valid_trials_and_redoes_the_failed_connection(tmp_path):
    t = task()
    runner = EvalRunner(client_factory=lambda tk, rep: ScriptedClient(say("hi") if rep != 2 else RuntimeError("connection closed")), journal=None)
    runner.journal = Journal(tmp_path, runner.fingerprint([t], 3))
    first = runner.evaluate_task(t, 3)
    assert [x.passed for x in first.trials] == [True, False, True] and not trial_is_valid(first.trials[1]) and trial_is_valid(first.trials[0])
    made = []

    def factory(tk, rep):
        made.append(rep)
        return ScriptedClient(say("hi"))
    again = EvalRunner(client_factory=factory)
    again.journal = Journal(tmp_path, again.fingerprint([t], 3))
    done = again.evaluate_task(t, 3)
    assert made == [2] and [x.passed for x in done.trials] == [True, True, True] and again.journal.reused == 2
    other = EvalRunner(client_factory=factory, temperature=0.1)
    with pytest.raises(ResumeMismatch, match="sampling.temperature"):
        Journal(tmp_path, other.fingerprint([t], 3))


def test_a_trial_survives_the_trip_through_the_journal(tmp_path):
    j = Journal(tmp_path, fingerprint())
    trial = Trial(repetition=1, passed=True, tool_calls=2, trace=[{"name": "a", "args": "{}", "key": "k"}], components={"skills": 1})
    from dataclasses import asdict
    j.add(("t", 1), asdict(trial), True)
    assert Trial(**Journal(tmp_path, fingerprint()).get(("t", 1))) == trial
