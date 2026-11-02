"""The SWE-bench Verified adapter (tadamoo/bench/adapters/swe_bench.py): the dataset in the imported folder, the subsets, the container, the two arms and the
reading of the official harness's report, with a fake Docker and a fake `swebench eval`. No Docker, no GPU, no model."""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_support import GIT_ENV, MODEL, make_spec
from tadamoo.bench import analysis
from tadamoo.bench.adapters import swe_bench as sb
from tadamoo.bench.adapters.containerkit import ExecOut
from tadamoo.bench.importer import Result
from tadamoo.bench.runner import BenchRun, TrialContext, TrialResult, get_adapter
from test_session import Scripted, call, say

BASH = shutil.which("bash")
if BASH and "system32" in BASH.lower().replace("/", "\\"):
    BASH = None
needs_bash = pytest.mark.skipif(BASH is None or shutil.which("git") is None, reason="needs bash and git to stand in for the container")

DIFFICULTIES = ["<15 min fix", "15 min - 1 hour", "1-4 hours", ">4 hours"]


def instances(n=30):
    return [{"instance_id": f"proj__proj-{i:03d}", "repo": "proj/proj", "problem_statement": f"Issue {i}: a.py returns the wrong value.", "difficulty": DIFFICULTIES[i % 4],
             "FAIL_TO_PASS": "[\"t\"]", "PASS_TO_PASS": "[]", "image": f"example/sweb.eval.proj-{i:03d}:latest"} for i in range(1, n + 1)]


def make_bench(base: Path, rows=None, name="test.jsonl") -> Path:
    bench = base / "swe-bench-verified"
    data = bench / "source" / "data"
    data.mkdir(parents=True)
    rows = rows or instances()
    (data / name).write_text(json.dumps(rows) if name.endswith(".json") else "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    (bench / "source" / "README.md").write_text("# data\n", encoding="utf-8")
    return bench


# -- the dataset and the subsets ---------------------------------------------------------------------------------------

def test_the_dataset_is_read_from_the_imported_folder(tmp_path):
    bench = make_bench(tmp_path)
    rows = sb.load_instances(sb.source_of(bench))
    assert len(rows) == 30 and rows[0]["instance_id"] == "proj__proj-001"
    as_json = make_bench(tmp_path / "j", rows=instances(3), name="verified.json")
    assert len(sb.load_instances(sb.source_of(as_json))) == 3
    with pytest.raises(FileNotFoundError, match="import the benchmark"):
        sb.load_instances(tmp_path / "empty")


def test_a_parquet_dataset_needs_pyarrow_and_a_git_lfs_pointer_is_named(tmp_path, monkeypatch):
    bench = tmp_path / "swe-bench-verified"
    data = bench / "source" / "data"
    data.mkdir(parents=True)
    pointer = data / "test-00000-of-00001.parquet"
    pointer.write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 10\n")
    with pytest.raises(FileNotFoundError, match="Git LFS"):
        sb.load_instances(sb.source_of(bench))
    pointer.write_bytes(b"PAR1 pretend")
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
    seen = []

    def runner(args, cwd=None, timeout=None, env=None):             # `uv run --with pyarrow python script out.jsonl files...` writes the rows
        seen.append(list(args))
        Path(args[args.index("python") + 2]).write_text(json.dumps(instances(1)[0]) + "\n", encoding="utf-8")
        return Result(0, "", "")
    rows = sb.load_instances(sb.source_of(bench), runner=runner, which=lambda n: "/bin/uv")
    assert rows[0]["instance_id"] == "proj__proj-001" and seen[0][:5] == ["/bin/uv", "run", "--with", "pyarrow", "python"]
    again = sb.load_instances(sb.source_of(bench), runner=lambda *a, **k: Result(1, "", "must not run again"), which=lambda n: "/bin/uv")
    assert len(again) == 1                                          # the converted file is kept beside the clone


def test_subsets_are_seeded_nested_and_stable():
    rows = instances(500)
    smoke, dev, hundred = (sb.select_instances(rows, s) for s in ("smoke", "dev", "random100"))
    assert (len(smoke), len(dev), len(hundred)) == (2, 20, 100)
    assert dev == hundred[:20] and len(set(hundred)) == 100
    assert all(rows[int(i.rsplit("-", 1)[1]) - 1]["difficulty"] == "<15 min fix" for i in smoke)
    assert sb.select_instances(rows, "random100") == hundred
    assert sb.select_instances(rows, "random100", {"seed": 7}) != hundred
    assert sb.select_instances(rows, "dev-baseline") == dev           # the arm is not part of the choice of instances
    assert sb.select_instances(rows, "full") == sorted(r["instance_id"] for r in rows)
    assert sb.select_instances(rows, "x", {"instances": [rows[3]["instance_id"]]}) == [rows[3]["instance_id"]]
    with pytest.raises(ValueError, match="no such SWE-bench instance"):
        sb.select_instances(rows, "dev", {"instances": ["nope"]})
    with pytest.raises(ValueError, match="unknown subset"):
        sb.select_instances(rows, "huge")


def test_the_arm_comes_from_the_setting_or_the_subset():
    assert sb.arm_of({"subset": "dev"}) == "harness" and sb.arm_of({"subset": "dev-baseline"}) == "baseline"
    assert sb.arm_of({"subset": "dev-baseline", "arm": "harness"}) == "harness"
    with pytest.raises(ValueError, match="'harness' or 'baseline'"):
        sb.arm_of({"arm": "gpt"})


def test_the_registry_offers_both_arms_as_subsets():
    from tadamoo.bench import registry
    ids = {s.id for s in registry.get("swe-bench-verified").subsets}
    assert {"smoke", "dev", "random100", "full", "smoke-baseline", "dev-baseline", "random100-baseline", "full-baseline"} <= ids
    assert set(sb.SweBenchAdapter.SETTINGS["subset"]) == ids


def test_plan_gives_each_instance_its_repetitions(tmp_path):
    bench = make_bench(tmp_path)
    a = sb.SweBenchAdapter()
    keys = a.plan({"subset": "dev", "trials": 2}, bench)
    assert len(keys) == 40 and keys[0][1] == 1 and keys[1] == (keys[0][0], 2)
    assert [k[0] for k in a.plan({"subset": "dev-baseline"}, bench)] == [k[0] for k in a.plan({"subset": "dev"}, bench)]


def test_the_image_is_the_datasets_own_or_the_published_name():
    assert sb.image_of({"instance_id": "a__b-1", "image": "x/y:z"}) == "x/y:z"
    assert sb.image_of({"instance_id": "Django__Django-11099"}) == "swebench/sweb.eval.x86_64.django_1776_django-11099:latest"


# -- Docker and the patch ----------------------------------------------------------------------------------------------

def test_the_container_is_driven_with_docker_commands():
    calls = []

    def runner(args, cwd=None, timeout=None, env=None):
        calls.append((list(args), timeout))
        if args[1:3] == ["image", "inspect"]:
            return Result(1, "", "no such image")
        if args[1] == "exec" and "sleep" in args[-1]:
            return Result(124, "", "docker: timed out")
        return Result(0, "out", "err")
    c = sb.DockerContainer("img:1", "name1", runner)
    assert not c.have_image()
    c.pull()
    c.start()
    assert c.started and calls[2][0][:8] == ["docker", "run", "-d", "--name", "name1", "-w", "/testbed", "--user"] and calls[2][0][-4:] == ["img:1", "tail", "-f", "/dev/null"]
    out = c.exec("echo hi", 30)
    assert (out.rc, out.stdout, out.stderr, out.timed_out) == (0, "out", "err", False)
    last = calls[-1][0]
    assert last[:5] == ["docker", "exec", "-w", "/testbed", "name1"] and last[5:7] == ["bash", "-c"] and last[7].endswith("echo hi") and "conda activate testbed" in last[7]
    slow = c.exec("sleep 99", 1)
    assert slow.timed_out and slow.rc == 124
    c.remove()
    assert calls[-1][0] == ["docker", "rm", "-f", "name1"] and not c.started
    failing = sb.DockerContainer("img:1", "n", lambda *a, **k: Result(1, "", "denied"))
    with pytest.raises(RuntimeError, match="docker pull img:1 failed: denied"):
        failing.pull()
    with pytest.raises(RuntimeError, match="docker run img:1 failed"):
        failing.start()


def test_the_patch_leaves_out_binary_files():
    seen = []

    def exec_fn(command, timeout=None):
        seen.append(command)
        if "numstat" in command:
            return ExecOut(0, "3\t1\tsrc/a.py\n-\t-\tdocs/pic one.png\n")
        if command.startswith("git diff --cached --no-color"):
            return ExecOut(0, "diff --git a/src/a.py b/src/a.py\n")
        return ExecOut(0)
    assert sb.git_patch(exec_fn).startswith("diff --git a/src/a.py")
    assert seen[0] == "git add -A" and "':(exclude)docs/pic one.png'" in seen[-1]
    assert sb.git_patch(lambda c, t=None: ExecOut(1, "", "not a git repository")) == ""


# -- a trial, with a local repository as the container -----------------------------------------------------------------

class LocalContainer:
    """The container as a folder with a git repository; the folder stands in for /testbed."""

    def __init__(self, repo: Path, log):
        self.repo, self.log, self.image, self.name, self.removed, self.started = repo, log, "img", "n", False, False

    def have_image(self):
        self.log.append("have_image")
        return True

    def pull(self):
        self.log.append("pull")

    def start(self):
        self.started = True

    def exec(self, command, timeout=None):
        p = subprocess.run([BASH, "-c", command.replace("/testbed", self.repo.as_posix())], cwd=self.repo, capture_output=True, timeout=timeout or 60, env=GIT_ENV)
        return ExecOut(p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"))

    def remove(self):
        self.removed = True


def make_repo(base: Path) -> Path:
    repo = base / "repo"
    repo.mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "t@e.org"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, env=GIT_ENV, capture_output=True)
    (repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=GIT_ENV, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True, env=GIT_ENV, capture_output=True)
    return repo


class FakeEval:
    """`swebench eval`: records the command and writes the logs the official harness would, by scenario."""

    def __init__(self, scenario="resolved"):
        self.scenario, self.calls = scenario, []

    def __call__(self, args, cwd=None, env=None, timeout=None, should_stop=None, log_path=None):
        self.calls.append({"args": list(args), "cwd": Path(cwd), "timeout": timeout, "patch": (Path(cwd) / "predictions.jsonl").read_text()})
        iid = args[args.index("-i") + 1]
        run = args[args.index("-r") + 1]
        base = Path(cwd) / "logs" / "evaluation" / run / "basalt-x" / iid
        base.mkdir(parents=True)
        if self.scenario in ("resolved", "unresolved"):
            (base / "report.json").write_text(json.dumps({iid: {"resolved": self.scenario == "resolved", "patch_successfully_applied": True}}), encoding="utf-8")
            (base / "test_output.txt").write_text("1 failed, 2 passed", encoding="utf-8")
        elif self.scenario == "infra":
            (base / "report.json").write_text(json.dumps({iid: {"resolved": False, "infra_failure": True, "infra_failure_reason": "container_unavailable"}}), encoding="utf-8")
        elif self.scenario == "apply_failed":
            (base / "run_instance.log").write_text(">>>>> Patch Apply Failed:\nerror: patch failed", encoding="utf-8")
        return Result(0 if self.scenario != "crash" else 1, "ok", "")


def adapter_with(repo, evaluator, client, log=None):
    log = log if log is not None else []
    box = {}

    def factory(image, name):
        box["c"] = LocalContainer(repo, log)
        return box["c"]
    a = sb.SweBenchAdapter(eval_runner=evaluator, which=lambda n: "/bin/swebench", client_factory=lambda endpoint: client, container_factory=factory)
    return a, box, log


def trial_ctx(tmp_path, settings, stop=lambda: False):
    bench = make_bench(tmp_path)
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "settings.json").write_text(json.dumps({"model": MODEL, "settings": settings}), encoding="utf-8")
    return TrialContext(run_dir, run_dir / "trials" / "t", bench, settings, stop)


@needs_bash
def test_the_baseline_arm_edits_in_the_container_and_the_official_harness_grades_the_patch(tmp_path):
    repo = make_repo(tmp_path)
    client = Scripted(call("bash", command="sed -i 's/return 1/return 2/' a.py && echo done"), say("fixed"))
    ev = FakeEval("resolved")
    a, box, _log = adapter_with(repo, ev, client)
    ctx = trial_ctx(tmp_path, {"subset": "dev-baseline", "agent_timeout_s": 60})
    res = a.run_trial(("proj__proj-002", 1), ctx)
    assert res.passed is True and res.score == 1.0 and res.facts["arm"] == "baseline" and res.facts["turns"] == 2
    assert box["c"].removed and "return 2" in (ctx.trial_dir / "model.patch").read_text()
    call_ = ev.calls[0]
    assert call_["args"][:4] == ["/bin/swebench", "eval", "instance.json", "-p"] and call_["args"][call_["args"].index("-i") + 1] == "proj__proj-002"
    assert json.loads(call_["patch"])["model_patch"].count("+    return 2") == 1 and json.loads(call_["patch"])["model_name_or_path"] == "basalt-baseline"
    assert json.loads((call_["cwd"] / "instance.json").read_text())[0]["instance_id"] == "proj__proj-002"
    assert res.verifier_output_path.endswith("proj__proj-002/test_output.txt") and res.trajectory_path == "trials/t/trajectory.jsonl"
    prompt = client.sent[0][1]["content"]
    assert "Issue 2: a.py returns the wrong value." in prompt and "/testbed" in prompt
    assert client.sent[0][0]["role"] == "system" and len(client.sent[0]) == 2


@needs_bash
def test_the_harness_arm_uses_our_agent_with_container_tools(tmp_path, monkeypatch):
    monkeypatch.setattr("tadamoo.stack.local_now", None)
    repo = make_repo(tmp_path)
    client = Scripted(call("edit_file", path="a.py", target="return 1", replacement="return 2"), say("Fixed it."))
    ev = FakeEval("unresolved")
    a, _box, _log = adapter_with(repo, ev, client)
    ctx = trial_ctx(tmp_path, {"subset": "dev", "max_steps": 20})
    res = a.run_trial(("proj__proj-002", 1), ctx)
    assert res.passed is False and res.facts["arm"] == "harness" and (repo / "a.py").read_text() == "def f():\n    return 2\n"
    assert json.loads(ev.calls[0]["patch"])["model_name_or_path"] == "basalt-harness"
    system = client.sent[0][0]["content"]
    assert "the instance's Linux container" in system and "/testbed" in system
    events = [json.loads(l) for l in (ctx.trial_dir / "trajectory.jsonl").read_text().splitlines()]
    assert [e["type"] for e in events if e["type"] in ("tool_call", "stop")] == ["tool_call", "stop"] and res.tokens == 0


@needs_bash
def test_an_empty_patch_is_unresolved_without_running_the_evaluation(tmp_path):
    repo = make_repo(tmp_path)
    ev = FakeEval("resolved")
    a, _box, _log = adapter_with(repo, ev, Scripted(say("I looked and changed nothing.")))
    res = a.run_trial(("proj__proj-002", 1), trial_ctx(tmp_path, {"subset": "dev-baseline"}))
    assert res.passed is False and res.facts["empty_patch"] and ev.calls == []
    assert "no change" in (tmp_path / "runs" / "r1" / res.verifier_output_path).read_text()


@needs_bash
@pytest.mark.parametrize("scenario,passed,fragment", [("infra", None, "container_unavailable"), ("apply_failed", False, ""), ("crash", None, "left no report")])
def test_the_reports_of_the_official_harness_are_read_as_it_means_them(tmp_path, scenario, passed, fragment):
    repo = make_repo(tmp_path)
    a, _box, _log = adapter_with(repo, FakeEval(scenario), Scripted(call("bash", command="echo x >> a.py"), say("ok")))
    res = a.run_trial(("proj__proj-002", 1), trial_ctx(tmp_path, {"subset": "dev-baseline"}))
    assert res.passed is passed and fragment in res.error
    if scenario == "apply_failed":
        assert res.facts["patch_applied"] is False and res.error == ""


@needs_bash
def test_a_dead_model_server_leaves_the_trial_unjudged(tmp_path):
    repo = make_repo(tmp_path)
    ev = FakeEval("resolved")
    a, box, _log = adapter_with(repo, ev, Scripted(RuntimeError("cannot reach the Basalt server at http://127.0.0.1:7232: refused")))
    res = a.run_trial(("proj__proj-002", 1), trial_ctx(tmp_path, {"subset": "dev-baseline"}))
    assert res.passed is None and "cannot reach the Basalt server" in res.error and ev.calls == [] and box["c"].removed


@needs_bash
def test_the_image_is_pulled_only_when_missing_and_only_if_allowed(tmp_path):
    repo = make_repo(tmp_path)
    log = []
    a, box, _ = adapter_with(repo, FakeEval(), Scripted(say("nothing")), log)
    a.run_trial(("proj__proj-002", 1), trial_ctx(tmp_path, {"subset": "dev-baseline"}))
    assert log == ["have_image"]

    class Missing(LocalContainer):
        def have_image(self):
            return False
    a2 = sb.SweBenchAdapter(eval_runner=FakeEval(), client_factory=lambda e: Scripted(say("x")), container_factory=lambda i, n: Missing(repo, log))
    res = a2.run_trial(("proj__proj-002", 1), trial_ctx(tmp_path / "b", {"subset": "dev-baseline", "pull_images": False}))
    assert res.passed is None and "pull_images is off" in res.error


def test_a_stop_request_leaves_the_trial_unjudged(tmp_path):
    class Stopping(LocalContainer):
        def exec(self, command, timeout=None):
            return ExecOut(0)
    repo = tmp_path / "repo"
    repo.mkdir()
    def slow_first():                                               # the stop watch looks once a second: the request lands while the first reply is awaited
        time.sleep(1.5)
        return call("bash", command="true")
    a = sb.SweBenchAdapter(eval_runner=FakeEval(), client_factory=lambda e: Scripted(slow_first, say("x")), container_factory=lambda i, n: Stopping(repo, []))
    res = a.run_trial(("proj__proj-002", 1), trial_ctx(tmp_path, {"subset": "dev-baseline"}, stop=lambda: True))
    assert res.passed is None and "stopped" in res.error


def test_score_failure_facts_and_the_endpoint_guard(tmp_path):
    a = sb.SweBenchAdapter()
    m = a.score({("a", 1): TrialResult(passed=True, facts={"arm": "harness"}), ("b", 1): TrialResult(passed=False, facts={"arm": "harness", "empty_patch": True}),
                 ("c", 1): TrialResult(passed=None, error="x")})
    assert m == {"resolution_rate": 0.5, "resolved": 1, "of": 2, "invalid": 1, "empty_patches": 1, "arm": "harness"}
    assert a.failure_facts(TrialResult(error="e", facts={"arm": "baseline"})) == {"error": "e", "arm": "baseline"}
    ctx = trial_ctx(tmp_path, {"subset": "dev", "endpoint": "https://api.example.com"})
    (ctx.run_dir / "settings.json").write_text(json.dumps({"model": {}, "settings": ctx.settings}), encoding="utf-8")
    with pytest.raises(ValueError, match="not the local engine"):
        a.run_trial(("proj__proj-002", 1), ctx)


@needs_bash
def test_a_whole_run_of_both_arms_is_comparable(tmp_path, monkeypatch):
    """The adapter inside BenchRun twice (harness arm, baseline arm) and the comparison of the two runs."""
    monkeypatch.setattr("tadamoo.stack.local_now", None)
    from tadamoo.bench import ranking
    spec = make_spec("unused", id="swe-bench-verified", primary_metric="resolution_rate")
    summaries = []
    for arm, resolved in (("harness", "resolved"), ("baseline", "unresolved")):
        repo = make_repo(tmp_path / arm)
        client = Scripted(call("bash", command="echo x >> a.py"), say("ok")) if arm == "baseline" else Scripted(call("write_file", path="b.py", content="x = 1\n"), say("ok"))
        a, _box, _ = adapter_with(repo, FakeEval(resolved), client)
        subset = "dev-baseline" if arm == "baseline" else "dev"
        run = BenchRun(tmp_path / arm / "base", spec, {"subset": subset, "instances": ["proj__proj-002"], "trials": 1}, adapter=a, model=MODEL, power_limit_w=450.0, root=tmp_path)
        shutil.copytree(make_bench(tmp_path / arm / "data"), tmp_path / arm / "base" / "swe-bench-verified", dirs_exist_ok=True)
        state = run.execute()
        assert state["status"] == "done", state
        rep = analysis.report(run.dir, a)
        assert rep["trials"] == 1
        summaries.append(ranking.run_summary(run.dir))
    assert [s["subset"] for s in summaries] == ["dev", "dev"] and [s["arm"] for s in summaries] == ["", "baseline"]
    verdict = ranking.compare_runs("swe-bench-verified", summaries)
    assert verdict["winner"] is None and verdict["ordered"][0]["value"] == 1.0           # one instance each: far too little to name a winner


def test_the_adapter_is_found_without_being_listed():
    assert get_adapter("swe-bench-verified", lazy=True).id == "swe-bench-verified"
